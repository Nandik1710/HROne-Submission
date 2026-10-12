"""Employee Attendance API. Run: uvicorn app.main:app --port 8000."""
import os
import json
from calendar import monthrange
from bson import Decimal128, json_util
import re
from contextlib import asynccontextmanager
from datetime import date as calendar_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
Status = Literal['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']
PresenceStatus = Literal['PRESENT', 'WFH', 'ON_DUTY']
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
ShiftTime = Annotated[str, Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def valid_date(value: str) -> str:
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('Expected YYYY-MM-DD')
    calendar_date.fromisoformat(value)
    return value


DateString = Annotated[str, AfterValidator(valid_date)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r'^EMP\d{4,6}$')
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = '09:30'
    shift_end: ShiftTime = '18:30'
    joined_on: DateString

    @model_validator(mode='after')
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError('shift_start must differ from shift_end')
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    # Omitted is allowed; explicit null is not in the request schema.
    punched_at: EpochMillis = Field(default_factory=lambda: to_millis(now()))


class PunchInIn(PunchOutIn):
    status: PresenceStatus = 'PRESENT'


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Status = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    # Defaults mark omitted fields; explicit null still fails validation.


client = MongoClient(os.getenv('MONGO_URI', 'mongodb://localhost:27017'),
                     tz_aware=True, serverSelectionTimeoutMS=3000,
                     connectTimeoutMS=3000)
db = client[os.getenv('MONGO_DB', 'attendance_db')]


def create_indexes():
    db.employees.create_index('emp_code', unique=True)
    db.employees.create_index([('joined_on', 1), ('department', 1)])
    db.employees.create_index([('department', 1), ('emp_code', 1)])
    db.attendance_logs.create_index([('emp_code', 1), ('date', -1)], unique=True)
    db.attendance_logs.create_index([('date', -1), ('emp_code', 1)])
    db.attendance_logs.create_index([('status', 1), ('date', -1), ('emp_code', 1)])
    db.attendance_logs.create_index([('emp_code', 1), ('status', 1), ('date', -1)])
    db.attendance_logs.create_index([('emp_code', 1), ('punch_in', -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    try:
        yield
    finally:
        client.close()


app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0', lifespan=lifespan)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_millis(value: int) -> datetime:
    return EPOCH + timedelta(seconds=value // 1000)


def to_millis(value: datetime) -> int:
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def serialize(value):
    """Convert BSON dates even inside history; internal identifiers never escape."""
    if isinstance(value, datetime):
        return to_millis(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items() if k != '_id'}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    return value


DEFAULTS = {'punch_in': None, 'punch_out': None, 'work_hours': None,
            'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False}
CHANGE_FIELDS = ('status', *DEFAULTS)


def attendance_response(doc):
    return serialize({**DEFAULTS, 'history': [], **doc})


def invalid(message):
    raise HTTPException(422, detail=[{'loc': ['body'], 'msg': message, 'type': 'value_error'}])


def employee_or_404(emp_code):
    employee = db.employees.find_one({'emp_code': emp_code})
    if employee is None:
        raise HTTPException(404, 'Employee not found')
    return employee


def attendance_date(punch_in: datetime, employee) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if employee['shift_end'] <= employee['shift_start'] and local.strftime('%H:%M') < employee['shift_end']:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day: str, employee):
    start = datetime.fromisoformat(day + 'T' + employee['shift_start']).replace(tzinfo=IST)
    end = datetime.fromisoformat(day + 'T' + employee['shift_end']).replace(tzinfo=IST)
    if employee['shift_end'] <= employee['shift_start']:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, shift_start):
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end):
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def recompute(doc, employee):
    if doc['status'] in ('ABSENT', 'LEAVE'):
        doc.update(DEFAULTS)
        return
    if doc.get('punch_in') is None:
        invalid('A presence status requires punch_in')
    # Seeded dates can have milliseconds too; all corrected instants follow R1.
    for field in ('punch_in', 'punch_out'):
        if doc.get(field) is not None:
            doc[field] = doc[field].replace(microsecond=0)
    if attendance_date(doc['punch_in'], employee) != doc['date']:
        invalid('punch_in must stay on the attendance date')
    start, end = shift_bounds(doc['date'], employee)
    doc.update(work_hours=None, late_minutes=compute_late_minutes(doc['punch_in'], start),
               overtime_minutes=0, half_day=False)
    if doc.get('punch_out') is not None:
        elapsed = doc['punch_out'] - doc['punch_in']
        if not timedelta(0) < elapsed <= timedelta(hours=24):
            invalid('punch_out must be after punch_in and within 24 hours')
        doc['work_hours'] = compute_work_hours(doc['punch_in'], doc['punch_out'])
        doc['overtime_minutes'] = compute_overtime(doc['punch_out'], end)
        doc['half_day'] = doc['work_hours'] < 4.50


def snapshot_filter(original):
    """Compare the full mutable snapshot, including history, without schema additions.

    MongoDB checks this predicate and applies the update atomically. Comparing history
    also detects a record changed and then changed back by another correction.
    """
    query = {'emp_code': original['emp_code'], 'date': original['date']}
    for field in (*CHANGE_FIELDS, 'history'):
        query[field] = {'$eq': original[field]} if field in original else {'$exists': False}
    return query


def update_record(original, revised, history_entry=None):
    update = {'$set': {field: revised[field] for field in CHANGE_FIELDS}}
    if history_entry is not None:
        update['$push'] = {'history': history_entry}
    saved = db.attendance_logs.find_one_and_update(snapshot_filter(original), update,
                                                  return_document=ReturnDocument.AFTER)
    if saved is None:
        raise HTTPException(409, 'Attendance changed concurrently; reload and retry')
    return attendance_response(saved)


@app.get('/health')
def health():
    try:
        db.command('ping')
    except PyMongoError:
        raise HTTPException(503, 'MongoDB unavailable')
    return {'status': 'ok'}


@app.post('/employees', status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc['created_at'] = now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'emp_code already exists')
    return serialize(doc)


@app.get('/employees')
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {'department': department}
    items = list(db.employees.find(query, {'_id': 0}).sort('emp_code', 1)
                 .skip((page - 1) * page_size).limit(page_size))
    return {'items': serialize(items), 'total': db.employees.count_documents(query),
            'page': page, 'page_size': page_size}


@app.post('/attendance/punch-in', status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    instant = from_millis(body.punched_at)
    doc = {**DEFAULTS, 'emp_code': body.emp_code, 'date': attendance_date(instant, employee),
           'status': body.status, 'punch_in': instant, 'history': []}
    recompute(doc, employee)
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'Attendance already exists for this date')
    return attendance_response(doc)


@app.post('/attendance/punch-out')
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    instant = from_millis(body.punched_at)
    original = db.attendance_logs.find_one(
        {'emp_code': body.emp_code, 'punch_in': {'$lte': instant, '$type': 'date'}},
        sort=[('punch_in', -1)])
    if original is None:
        raise HTTPException(404, 'No punch-in found at or before punched_at')
    if original.get('punch_out') is not None:
        raise HTTPException(409, 'Already punched out')
    revised = {**DEFAULTS, **original, 'punch_out': instant}
    recompute(revised, employee)
    return update_record(original, revised)


@app.get('/attendance')
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    command = attendance_command(emp_code, date_from, date_to, status, page, page_size)
    items = db.attendance_logs.find(command['filter'], command['projection']).sort(
        list(command['sort'].items())).hint(command['hint']).skip(command['skip']).limit(command['limit'])
    return {'items': [attendance_response(doc) for doc in items],
            'total': db.attendance_logs.count_documents(command['filter'], hint=command['hint']),
            'page': page, 'page_size': page_size}


@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    employee = employee_or_404(emp_code)
    original = db.attendance_logs.find_one({'emp_code': emp_code, 'date': date})
    if original is None:
        raise HTTPException(404, 'Attendance not found')
    supplied = body.model_dump(exclude_unset=True)
    if supplied.get('status') in ('ABSENT', 'LEAVE') and {'punch_in', 'punch_out'} & supplied.keys():
        invalid('ABSENT or LEAVE cannot be supplied with punch times')
    revised = {**DEFAULTS, **original}
    for field in ('status', 'punch_in', 'punch_out'):
        if field in supplied:
            revised[field] = from_millis(supplied[field]) if field != 'status' else supplied[field]
    recompute(revised, employee)
    changes = {field: {'from': original.get(field, DEFAULTS.get(field)), 'to': revised[field]}
               for field in CHANGE_FIELDS if original.get(field, DEFAULTS.get(field)) != revised[field]}
    if not changes:
        invalid('Correction must change at least one field')
    entry = {'at': now(), 'by': body.regularized_by, 'reason': body.reason, 'changes': changes}
    return update_record(original, revised, entry)



# Analytics builders return the exact MongoDB command used for execution/explain.
# All calculations below are aggregation expressions, evaluated by MongoDB.

def valid_month(value: str) -> str:
    if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('Expected YYYY-MM')
    calendar_date.fromisoformat(value + '-01')
    return value


MonthString = Annotated[str, AfterValidator(valid_month)]
LeaderboardLimit = Annotated[int, Query(ge=1, le=50)]
ExplainEndpoint = Literal['attendance_list', 'employee_monthly', 'department_summary',
                          'late_leaderboard', 'department_trend']
PRESENCE = ['PRESENT', 'WFH', 'ON_DUTY']


def month_bounds(month):
    year, number = map(int, month.split('-'))
    return month + '-01', f'{month}-{monthrange(year, number)[1]:02d}'


def mongo_date(value):
    return {'$dateFromString': {'dateString': value, 'timezone': '+05:30'}}


def weekday(value):
    return {'$lte': [{'$isoDayOfWeek': {'date': mongo_date(value), 'timezone': '+05:30'}}, 5]}


def calendar_days(start, end):
    """Build a bounded calendar in the database, even when there are no logs."""
    first = mongo_date({'$literal': start})
    count = (calendar_date.fromisoformat(end) - calendar_date.fromisoformat(start)).days + 1
    return {'$map': {'input': {'$range': [0, count]}, 'as': 'offset', 'in': {
        '$dateToString': {'format': '%Y-%m-%d', 'timezone': '+05:30', 'date': {
            '$dateAdd': {'startDate': first, 'unit': 'day', 'amount': '$$offset'}}}}}}


def half_up(value, places=2):
    """Nonnegative attendance metrics: decimal floor(x*scale + .5)/scale."""
    scale = 10 ** places
    return {'$cond': [{'$eq': [{'$ifNull': [value, None]}, None]}, None,
        {'$toDouble': {'$divide': [{'$floor': {'$add': [
            {'$multiply': [{'$toDecimal': value}, scale]}, Decimal128('0.5')]}}, scale]}}]}


def ratio(numerator, denominator, places=2, multiplier=1):
    return {'$cond': [{'$gt': [denominator, 0]}, half_up({'$divide': [
        {'$multiply': [{'$toDecimal': numerator}, multiplier]}, denominator]}, places), None]}


def present_weight(weekdays_only=False):
    condition = {'$in': ['$status', PRESENCE]}
    if weekdays_only:
        condition = {'$and': [condition, weekday('$date')]}
    return {'$cond': [condition, {'$cond': [{'$ifNull': ['$half_day', False]}, 0.5, 1]}, 0]}


def late_flag():
    return {'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}


def monthly_log_group():
    qualified_hours = {'$and': [{'$in': ['$status', PRESENCE]},
                                {'$ne': [{'$ifNull': ['$work_hours', None]}, None]}]}
    return {'$group': {
        '_id': None,
        'present_days': {'$sum': present_weight(True)},
        'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}},
        'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$status', 'ON_DUTY']}, 1, 0]}},
        'late_count': {'$sum': {'$cond': [late_flag(), 1, 0]}},
        'total_late_minutes': {'$sum': {'$cond': [late_flag(), '$late_minutes', 0]}},
        'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}},
        'hours_sum': {'$sum': {'$cond': [qualified_hours, {'$toDecimal': '$work_hours'}, 0]}},
        'hours_count': {'$sum': {'$cond': [qualified_hours, 1, 0]}},
    }}


def logs_lookup(start, end, stages):
    return {'$lookup': {'from': 'attendance_logs', 'let': {'code': '$emp_code'}, 'as': 'logs',
        'pipeline': [{'$match': {'date': {'$gte': start, '$lte': end},
                                 '$expr': {'$eq': ['$emp_code', '$$code']}}}, *stages]}}


def aggregate_command(collection, pipeline, hint):
    return {'aggregate': collection, 'pipeline': pipeline, 'cursor': {},
            'hint': hint, 'allowDiskUse': True}


def aggregate_rows(command):
    return list(db[command['aggregate']].aggregate(command['pipeline'],
                hint=command['hint'], allowDiskUse=command['allowDiskUse']))


def attendance_command(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid('date_from must not exceed date_to')
    query = {}
    if emp_code is not None:
        query['emp_code'] = emp_code
    if status is not None:
        query['status'] = status
    if date_from is not None or date_to is not None:
        query['date'] = {}
        if date_from is not None:
            query['date']['$gte'] = date_from
        if date_to is not None:
            query['date']['$lte'] = date_to
    if emp_code is not None:
        hint = 'emp_code_1_status_1_date_-1' if status is not None else 'emp_code_1_date_-1'
    else:
        hint = 'status_1_date_-1_emp_code_1' if status is not None else 'date_-1_emp_code_1'
    return {'find': 'attendance_logs', 'filter': query, 'projection': {'_id': 0},
            'sort': {'date': -1, 'emp_code': 1}, 'skip': (page - 1) * page_size,
            'limit': page_size, 'hint': hint}


def employee_monthly_command(emp_code, month):
    start, end = month_bounds(month)
    fields = ['present_days', 'leave_days', 'late_count', 'total_late_minutes', 'total_overtime_minutes']
    pipeline = [
        {'$match': {'emp_code': emp_code}},
        logs_lookup(start, end, [monthly_log_group()]),
        {'$set': {'totals': {'$arrayElemAt': ['$logs', 0]},
                  'working_days': {'$size': {'$filter': {'input': calendar_days(start, end),
                    'as': 'day', 'cond': {'$and': [weekday('$$day'), {'$gte': ['$$day', '$joined_on']}]}}}}}},
        {'$project': {'_id': 0, 'emp_code': 1, 'month': {'$literal': month}, 'working_days': 1,
                      **{field: {'$ifNull': ['$totals.' + field, 0]} for field in fields}}},
        {'$set': {'attendance_pct': ratio('$present_days', '$working_days', multiplier=100)}},
    ]
    return aggregate_command('employees', pipeline, 'emp_code_1')


def department_summary_command(month, department=None):
    start, end = month_bounds(month)
    query = {'joined_on': {'$lte': end}}
    if department is not None:
        query['department'] = department
    fields = ['present_days', 'late_count', 'total_late_minutes', 'on_duty_count', 'hours_sum', 'hours_count']
    pipeline = [
        {'$match': query},
        logs_lookup(start, end, [monthly_log_group()]),
        {'$set': {'totals': {'$arrayElemAt': ['$logs', 0]}}},
        {'$group': {'_id': '$department', 'headcount': {'$sum': 1},
                    'leave_count': {'$sum': {'$ifNull': ['$totals.leave_days', 0]}},
                    **{field: {'$sum': {'$ifNull': ['$totals.' + field, 0]}} for field in fields}}},
        {'$project': {'_id': 0, 'department': '$_id', 'headcount': 1, 'present_days': 1,
                      'late_count': 1, 'total_late_minutes': 1, 'leave_count': 1, 'on_duty_count': 1,
                      'avg_work_hours': ratio('$hours_sum', '$hours_count')}},
        {'$sort': {'department': 1}},
    ]
    hint = 'joined_on_1_department_1' if department is None else 'department_1_emp_code_1'
    return aggregate_command('employees', pipeline, hint)


def late_leaderboard_command(month, limit=10, department=None):
    start, end = month_bounds(month)
    pipeline = [
        {'$match': {'date': {'$gte': start, '$lte': end}}},
        {'$group': {'_id': '$emp_code',
                    'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}},
                    'late_count': {'$sum': {'$cond': [late_flag(), 1, 0]}}}},
        {'$match': {'total_late_minutes': {'$gt': 0}}},
        {'$lookup': {'from': 'employees', 'localField': '_id', 'foreignField': 'emp_code', 'as': 'employee'}},
        {'$unwind': '$employee'},
    ]
    if department is not None:
        pipeline.append({'$match': {'employee.department': department}})
    pipeline.extend([
        {'$setWindowFields': {'sortBy': {'total_late_minutes': -1}, 'output': {'rank': {'$rank': {}}}}},
        {'$match': {'rank': {'$lte': limit}}},
        {'$project': {'_id': 0, 'emp_code': '$_id', 'name': '$employee.name',
                      'department': '$employee.department', 'rank': 1, 'total_late_minutes': 1, 'late_count': 1}},
        {'$sort': {'total_late_minutes': -1, 'emp_code': 1}},
    ])
    return aggregate_command('attendance_logs', pipeline, 'date_-1_emp_code_1')


def department_trend_command(department, start, end):
    days = (calendar_date.fromisoformat(end) - calendar_date.fromisoformat(start)).days + 1
    if not 1 <= days <= 92:
        invalid('Trend range must contain between 1 and 92 calendar days')
    # Expand each employee over a bounded calendar inside MongoDB. This supplies
    # empty days without needing a log as a $densify seed, including future joiners.
    pipeline = [
        {'$match': {'department': department}},
        logs_lookup(start, end, [{'$project': {'_id': 0, 'date': 1,
            'present_count': present_weight(), 'late_count': {'$cond': [late_flag(), 1, 0]}}}]),
        {'$project': {'joined_on': 1, 'logs': 1, 'day': calendar_days(start, end)}},
        {'$unwind': '$day'},
        {'$set': {'daily': {'$arrayElemAt': [{'$filter': {'input': '$logs', 'as': 'log',
                                                        'cond': {'$eq': ['$$log.date', '$day']}}}, 0]}}},
        {'$group': {'_id': '$day',
                    'headcount': {'$sum': {'$cond': [{'$lte': ['$joined_on', '$day']}, 1, 0]}},
                    'present_count': {'$sum': {'$ifNull': ['$daily.present_count', 0]}},
                    'late_count': {'$sum': {'$ifNull': ['$daily.late_count', 0]}}}},
        {'$project': {'_id': 0, 'date': '$_id', 'headcount': 1, 'present_count': 1, 'late_count': 1,
                      'is_working_day': weekday('$_id')}},
        {'$set': {'attendance_rate': {'$cond': ['$is_working_day',
                                              ratio('$present_count', '$headcount', places=4), None]}}},
        {'$setWindowFields': {'sortBy': {'date': 1}, 'output': {'moving_avg_7d': {
            '$avg': {'$toDecimal': '$attendance_rate'}, 'window': {'documents': [-6, 0]}}}}},
        {'$set': {'moving_avg_7d': half_up('$moving_avg_7d', 4)}},
        {'$sort': {'date': 1}},
    ]
    return aggregate_command('employees', pipeline, 'department_1_emp_code_1')


@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code: str, month: MonthString):
    rows = aggregate_rows(employee_monthly_command(emp_code, month))
    if not rows:
        raise HTTPException(404, 'Employee not found')
    return rows[0]


@app.get('/analytics/departments/summary')
def department_summary(month: MonthString, department: str | None = None):
    return {'month': month, 'items': aggregate_rows(department_summary_command(month, department))}


@app.get('/analytics/leaderboard/late')
def late_leaderboard(month: MonthString, limit: LeaderboardLimit = 10, department: str | None = None):
    return {'month': month, 'items': aggregate_rows(late_leaderboard_command(month, limit, department))}


@app.get('/analytics/departments/{department}/trend')
def department_trend(department: str, date_from: Annotated[DateString, Query(alias='from')],
                     date_to: Annotated[DateString, Query(alias='to')]):
    rows = aggregate_rows(department_trend_command(department, date_from, date_to))
    if not rows:
        raise HTTPException(404, 'Department not found')
    return {'department': department, 'items': rows}


@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None,
                     month: MonthString | None = None, department: str | None = None,
                     limit: LeaderboardLimit = 10, date_from: DateString | None = None,
                     date_to: DateString | None = None, status: Status | None = None,
                     from_day: Annotated[DateString | None, Query(alias='from')] = None,
                     to_day: Annotated[DateString | None, Query(alias='to')] = None,
                     page: Page = 1, page_size: PageSize = 20):
    if endpoint in ('employee_monthly', 'department_summary', 'late_leaderboard') and month is None:
        invalid('month is required')
    if endpoint == 'attendance_list':
        command = attendance_command(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == 'employee_monthly':
        if emp_code is None:
            invalid('emp_code is required')
        command = employee_monthly_command(emp_code, month)
    elif endpoint == 'department_summary':
        command = department_summary_command(month, department)
    elif endpoint == 'late_leaderboard':
        command = late_leaderboard_command(month, limit, department)
    else:
        if department is None or from_day is None or to_day is None:
            invalid('department, from and to are required')
        command = department_trend_command(department, from_day, to_day)
    result = db.command('explain', command, verbosity='executionStats')
    # Preserve the raw explain structure, including internal plan keys named _id.
    # Extended JSON represents BSON-only values such as Decimal128 in expressions.
    return {'endpoint': endpoint, 'collection': command.get('aggregate', command.get('find')),
            'explain': json.loads(json_util.dumps(result))}
