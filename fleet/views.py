from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib.auth.decorators import login_required
from .models import FleetVehicle, RepairLog, VehicleMovement, Driver
from django.contrib import messages
from django.utils import timezone
import json
from portal.activity_logger import log_activity


def _can_access_vehicle_movements(user):
    return user.is_superuser or user.system_role == 'MANAGER' or 'vehicle_movement' in (user.assigned_modules or [])


def _can_access_vehicle_profiles(user):
    return (
        user.is_superuser
        or user.system_role in ['MANAGER', 'PROJECT_MANAGER']
        or 'vehicle_profiles' in (user.assigned_modules or [])
        or 'total_vehicles' in (user.assigned_modules or [])
    )


def _can_access_breakdown_register(user):
    return (
        user.is_superuser
        or user.system_role in ['MANAGER', 'PROJECT_MANAGER']
        or 'breakdown_register' in (user.assigned_modules or [])
        or 'breakdown_entry' in (user.assigned_modules or [])
    )


def _movement_form_data(request):
    from portal.models import Employee

    vehicle_number = (request.POST.get('vehicle_number') or '').strip()
    driver_name = (request.POST.get('driver_name') or '').strip()
    movement_date = request.POST.get('movement_date')
    movement_time = request.POST.get('movement_time')
    shift = request.POST.get('shift')
    if not vehicle_number or not driver_name:
        raise ValueError('Vehicle number and driver name are required.')
    if not movement_date or not movement_time or shift not in dict(VehicleMovement.SHIFT_CHOICES):
        raise ValueError('Date, time and a valid shift are required.')

    # Soft link to fleet master / employee when a match exists (optional, for stats)
    vehicle = FleetVehicle.objects.filter(regn__iexact=vehicle_number).first() or \
              FleetVehicle.objects.filter(dno__iexact=vehicle_number).first()
    # Only real drivers should be tracked here. Ensure the typed driver exists
    # in Shift Management (Employee) so it appears there too, but note that not
    # every Shift Management employee is a driver.
    driver = Employee.objects.filter(name__iexact=driver_name).first()
    if not driver:
        driver = Employee.objects.create(name=driver_name, nationality='', designation='Driver', department='P&M')
    return {
        'vehicle_number': vehicle_number,
        'driver_name': driver_name,
        'vehicle': vehicle,
        'driver': driver,
        'movement_date': movement_date,
        'movement_time': movement_time,
        'shift': shift,
        'destination': request.POST.get('destination', '').strip(),
        'purpose': request.POST.get('purpose', '').strip(),
    }


def _sync_driver_shift(movement, user):
    """Push this movement's driver data into Shift Management automatically.

    Runs on every create/edit so vehicle drivers always appear there with
    their current shift and assigned vehicle (not every employee there is a
    driver, but every movement driver belongs in Shift Management)."""
    if not movement.driver:
        return
    movement.driver.current_shift = movement.shift
    movement.driver.assigned_vehicle = movement.vehicle
    movement.driver.entered_by = user
    if not movement.driver.designation:
        movement.driver.designation = 'Driver'
    movement.driver.save(update_fields=['current_shift', 'assigned_vehicle', 'entered_by', 'designation'])


def _filtered_movements(request):
    """Build a queryset from the current filters."""
    qs = VehicleMovement.objects.select_related('vehicle', 'driver', 'entered_by').filter(is_deleted=False)
    vehicle_q = (request.GET.get('vehicle', '') or request.GET.get('vehicle_number', '')).strip()
    driver_q = request.GET.get('driver', '').strip()
    date_from = request.GET.get('date_from', '') or request.GET.get('start_date', '')
    date_to = request.GET.get('date_to', '') or request.GET.get('end_date', '')
    shift_f = request.GET.get('shift', '').strip()
    if vehicle_q:
        qs = qs.filter(vehicle_number__icontains=vehicle_q)
    if driver_q:
        qs = qs.filter(driver_name__icontains=driver_q)
    if date_from:
        qs = qs.filter(movement_date__gte=date_from)
    if date_to:
        qs = qs.filter(movement_date__lte=date_to)
    if shift_f in ('Day', 'Night'):
        qs = qs.filter(shift=shift_f)
    destination_q = request.GET.get('destination', '').strip()
    if destination_q:
        qs = qs.filter(destination__icontains=destination_q)
    vtype_q = request.GET.get('vehicle_type', '').strip()
    if vtype_q:
        qs = qs.filter(vehicle__model_name__icontains=vtype_q)
    return qs


def _movement_to_dict(m):
    return {
        'id': m.id,
        'vehicle_number': m.vehicle_number,
        'driver_name': m.driver_name,
        'shift': m.shift,
        'movement_date': m.movement_date.strftime('%Y-%m-%d'),
        'movement_date_display': m.movement_date.strftime('%d %b %Y') if m.movement_date else '',
        'movement_time': m.movement_time.strftime('%H:%M') if m.movement_time else '',
        'movement_time_display': m.movement_time.strftime('%I:%M %p') if m.movement_time else '',
        'destination': m.destination or '',
        'purpose': m.purpose or '',
        'model_name': m.vehicle.model_name if m.vehicle else '',
        'entered_by': (m.entered_by.full_name or m.entered_by.username) if m.entered_by else 'System',
        'updated_at': m.updated_at.strftime('%d %b, %H:%M') if m.updated_at else '',
    }


@login_required
def vehicle_movements(request):
    from portal.models import Employee
    if not _can_access_vehicle_movements(request.user):
        messages.error(request, 'Vehicle Movement Register is not assigned to your account.')
        return redirect('dashboard')

    movements = _filtered_movements(request)
    now = timezone.localtime()

    # Suggestion lists: fleet master + every previously-used value (so a DEO/
    # newly-added vehicle/driver appears too)
    vehicle_set = {}
    for v in FleetVehicle.objects.filter(is_active=True).order_by('regn', 'dno'):
        val = v.regn or v.dno
        vehicle_set[val] = {'value': val, 'label': f"{v.dno}{' - ' + v.model_name if v.model_name else ''}"}
    for num in VehicleMovement.objects.exclude(vehicle_number='').values_list('vehicle_number', flat=True):
        if num not in vehicle_set:
            vehicle_set[num] = {'value': num, 'label': num}
    vehicle_list = sorted(vehicle_set.values(), key=lambda x: x['value'].lower())

    # Suggest only real drivers (ones used in movement records), not all
    # Shift Management employees (many of them are not drivers).
    driver_set = {}
    for dn in VehicleMovement.objects.exclude(driver_name='').values_list('driver_name', flat=True):
        if dn and dn not in driver_set:
            driver_set[dn] = dn
    driver_list = sorted(driver_set.values(), key=str.lower)

    destinations = list(VehicleMovement.objects.exclude(destination='').values_list('destination', flat=True).distinct().order_by('destination')[:50])
    vehicle_types = list(FleetVehicle.objects.exclude(model_name='').exclude(model_name__isnull=True).values_list('model_name', flat=True).distinct().order_by('model_name'))
    shift_f = request.GET.get('shift', '').strip()

    filters = {
        'vehicle': request.GET.get('vehicle', ''),
        'driver': request.GET.get('driver', ''),
        'date_from': request.GET.get('date_from', ''),
        'date_to': request.GET.get('date_to', ''),
        'shift': shift_f,
    }
    return render(request, 'fleet/vehicle_movements.html', {
        'movements': movements,
        'vehicle_list': vehicle_list,
        'driver_list': driver_list,
        'movements_json': json.dumps([_movement_to_dict(m) for m in movements]),
        'default_date': now.date().isoformat(),
        'default_time': now.strftime('%H:%M'),
        'destinations': destinations,
        'vehicle_types': vehicle_types,
        'filters': filters,
    })


@login_required
def movements_live_api(request):
    from django.http import JsonResponse
    if not _can_access_vehicle_movements(request.user):
        return JsonResponse({'rows': [], 'count': 0})
    qs = _filtered_movements(request)
    rows = [_movement_to_dict(m) for m in qs]
    return JsonResponse({'rows': rows, 'count': len(rows)})


@login_required
def movements_export(request):
    from django.http import HttpResponse
    import io
    import datetime as _dt
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')

    fmt = request.GET.get('format', 'excel')
    columns = [c for c in request.GET.get('columns', '').split(',') if c]
    if not columns:
        columns = ['vehicle_number', 'driver_name', 'shift', 'movement_date', 'movement_time', 'destination', 'purpose', 'entered_by', 'updated_at']

    COL_LABELS = {
        'vehicle_number': 'Vehicle Number',
        'driver_name': 'Driver Name',
        'shift': 'Shift',
        'movement_date': 'Date',
        'movement_time': 'Time',
        'destination': 'Destination',
        'purpose': 'Purpose / Remarks',
        'entered_by': 'Entered By',
        'updated_at': 'Last Updated',
    }
    header = [COL_LABELS[c] for c in columns]
    rows = []
    for m in _filtered_movements(request):
        d = _movement_to_dict(m)
        row = []
        for c in columns:
            if c == 'movement_date':
                row.append(d['movement_date_display'])
            elif c == 'movement_time':
                row.append(d['movement_time_display'])
            else:
                row.append(d.get(c, ''))
        rows.append(row)

    date_str = _dt.date.today().strftime('%Y-%m-%d')

    if fmt == 'pdf':
        # Print-ready HTML (browser "Save as PDF")
        html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Vehicle Movement Register</title>'
        html += """
        <style>
          body{font-family:'Segoe UI',Arial,sans-serif;font-size:11px;margin:20px;color:#1e293b}
          .hb{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;padding:16px 22px;border-radius:8px;margin-bottom:16px}
          .hb h1{margin:0;font-size:20px}
          .hb p{margin:6px 0 0;opacity:.92;font-size:12px}
          table{width:100%;border-collapse:collapse}
          th,td{border:1px solid #cbd5e1;padding:6px 8px;text-align:left}
          th{background:#f1f5f9;font-weight:700;color:#0f172a}
          tr:nth-child(even){background:#f8fafc}
          @media print{@page{size:landscape;margin:1cm}}
        </style></head><body>
        <div class="hb"><h1>Vehicle Movement Register</h1>
        <p>Generated on: __NOW__ | __COUNT__ record(s)</p></div>
        """
        html = html.replace('__NOW__', timezone.now().strftime('%d %b %Y, %I:%M %p')).replace('__COUNT__', str(len(rows)))
        if not rows:
            html += '<p><i>No data matching selected filters.</i></p>'
        else:
            html += '<table><tr>' + ''.join(f'<th>{h}</th>' for h in header) + '</tr>'
            for r in rows:
                html += '<tr>' + ''.join(f'<td>{str(c)}</td>' for c in r) + '</tr>'
            html += '</table>'
        html += '<script>window.onload=function(){window.print();}</script></body></html>'
        return HttpResponse(html)

    # Excel
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Vehicle Movements'
    last_col = len(header)
    last_letter = openpyxl.utils.get_column_letter(max(last_col, 1))
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 24
    if last_col > 2:
        prev_letter = openpyxl.utils.get_column_letter(last_col - 1)
        ws.merge_cells(f'A1:{prev_letter}1')
        ws.merge_cells(f'A2:{prev_letter}2')
        ws.merge_cells(f'{last_letter}1:{last_letter}2')
        logo = _tyre_logo_image(width=52, height=52)
        if logo is not None:
            ws.add_image(logo, f'{last_letter}1')
    else:
        ws.merge_cells(f'A1:{last_letter}1')
        ws.merge_cells(f'A2:{last_letter}2')
    ws['A1'] = 'VEHICLE MOVEMENT REGISTER'
    ws['A1'].font = Font(bold=True, size=13)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws['A2'] = f'Generated on {date_str} | {len(rows)} record(s)'
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    header_fill = PatternFill('solid', start_color='2563EB')
    for ci, h in enumerate(header, 1):
        cell = ws.cell(row=3, column=ci, value=h)
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF')
        cell.alignment = Alignment(horizontal='center')
    thin = Side(style='thin', color='CBD5E1')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for ri, r in enumerate(rows, start=4):
        for ci, val in enumerate(r, start=1):
            ws.cell(row=ri, column=ci, value=val)
    for cell in ws[3]:
        cell.border = border
    for ci in range(1, last_col + 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = 22
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Vehicle_Movement_Register_{date_str}.xlsx'
    wb.save(response)
    return response


@login_required
def delete_vehicle_movement(request, movement_id):
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')
    if request.method == 'POST':
        m = get_object_or_404(VehicleMovement, id=movement_id)
        m.delete()
        messages.success(request, 'Movement record deleted successfully.')
    return redirect('fleet:vehicle_movements')


@login_required
def movement_directory(request):
    """Dedicated section: browse real vehicles & drivers (added via movement
    register) with live search; click one to see its records with date filters."""
    from portal.models import Employee
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')

    # All vehicles: active fleet master + every vehicle number used in movements
    vehicle_set = {}
    for v in FleetVehicle.objects.filter(is_active=True).order_by('regn', 'dno'):
        val = v.regn or v.dno
        if val:
            vehicle_set[val] = {
                'name': val,
                'meta': f"{v.dno}{' - ' + v.model_name if v.model_name else ''}",
                'count': VehicleMovement.objects.filter(vehicle_number__iexact=val).count(),
            }
    for num in VehicleMovement.objects.exclude(vehicle_number='').values_list('vehicle_number', flat=True):
        if num and num not in vehicle_set:
            vehicle_set[num] = {
                'name': num,
                'meta': 'Vehicle',
                'count': VehicleMovement.objects.filter(vehicle_number__iexact=num).count(),
            }
    vehicles = sorted(vehicle_set.values(), key=lambda x: x['name'].lower())

    # Only real drivers: every driver name used in movement records.
    # Shift Management employees are NOT shown here (they may not be drivers).
    driver_set = {}
    for dn in VehicleMovement.objects.exclude(driver_name='').values_list('driver_name', flat=True):
        if dn and dn not in driver_set:
            emp = Employee.objects.filter(name__iexact=dn).first()
            driver_set[dn] = {
                'name': dn,
                'meta': f"Emp ID: {emp.emp_id}" if emp and emp.emp_id else 'Driver',
                'count': VehicleMovement.objects.filter(driver_name__iexact=dn).count(),
            }
    drivers = sorted(driver_set.values(), key=lambda x: x['name'].lower())

    movements = _filtered_movements(request)

    return render(request, 'fleet/movement_directory.html', {
        'vehicles': vehicles,
        'drivers': drivers,
        'movements': movements,
        'vehicles_json': json.dumps(vehicles),
        'drivers_json': json.dumps(drivers),
        'movements_json': json.dumps([_movement_to_dict(m) for m in movements]),
    })


@login_required
def directory_export(request):
    """Advanced export for Fleet Records: pick a vehicle/driver plus a date
    range and download the matching movement data as Excel or PDF."""
    from django.http import HttpResponse
    import datetime as _dt
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')

    vehicle_q = (request.GET.get('vehicle') or '').strip()
    driver_q = (request.GET.get('driver') or '').strip()
    date_from = request.GET.get('date_from') or ''
    date_to = request.GET.get('date_to') or ''
    shift_q = request.GET.get('shift') or ''
    destination_q = (request.GET.get('destination') or '').strip()
    vtype_q = (request.GET.get('vehicle_type') or '').strip()

    qs = VehicleMovement.objects.select_related('vehicle', 'driver', 'entered_by').filter(is_deleted=False).order_by('movement_date', 'movement_time')
    scope_bits = []
    if vehicle_q:
        qs = qs.filter(vehicle_number__iexact=vehicle_q)
        scope_bits.append(f'Vehicle: {vehicle_q}')
    if driver_q:
        qs = qs.filter(driver_name__iexact=driver_q)
        scope_bits.append(f'Driver: {driver_q}')
    if date_from:
        qs = qs.filter(movement_date__gte=date_from)
        scope_bits.append(f'From: {date_from}')
    if date_to:
        qs = qs.filter(movement_date__lte=date_to)
        scope_bits.append(f'To: {date_to}')
    if shift_q in ('Day', 'Night'):
        qs = qs.filter(shift=shift_q)
        scope_bits.append(f'Shift: {shift_q}')
    if destination_q:
        qs = qs.filter(destination__icontains=destination_q)
        scope_bits.append(f'Destination: {destination_q}')
    if vtype_q:
        qs = qs.filter(vehicle__model_name__icontains=vtype_q)
        scope_bits.append(f'Type: {vtype_q}')
    scope = ' | '.join(scope_bits) if scope_bits else 'All vehicles & drivers'

    header = ['Date', 'Time', 'Shift', 'Vehicle Number', 'Driver Name', 'Destination', 'Purpose / Remarks', 'Entered By']
    rows = []
    for m in qs:
        d = _movement_to_dict(m)
        rows.append([
            d['movement_date_display'], d['movement_time_display'], d['shift'],
            d['vehicle_number'], d['driver_name'],
            d['destination'] or '-', d['purpose'] or '-', d['entered_by'],
        ])

    date_str = _dt.date.today().strftime('%Y-%m-%d')
    fmt = request.GET.get('format', 'excel')

    if fmt == 'pdf':
        # Print-ready HTML (browser "Save as PDF")
        html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Fleet Records</title>'
        html += """
        <style>
          body{font-family:'Segoe UI',Arial,sans-serif;font-size:11px;margin:20px;color:#1e293b}
          .hb{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;padding:16px 22px;border-radius:8px;margin-bottom:16px}
          .hb h1{margin:0;font-size:20px}
          .hb p{margin:6px 0 0;opacity:.92;font-size:12px}
          table{width:100%;border-collapse:collapse}
          th,td{border:1px solid #cbd5e1;padding:6px 8px;text-align:left}
          th{background:#f1f5f9;font-weight:700;color:#0f172a}
          tr:nth-child(even){background:#f8fafc}
          @media print{@page{size:landscape;margin:1cm}}
        </style></head><body>
        <div class="hb"><h1>Fleet Records</h1>
        <p>__SCOPE__ &mdash; Generated on __NOW__ | __COUNT__ record(s)</p></div>
        """
        html = (html.replace('__SCOPE__', scope)
                    .replace('__NOW__', timezone.now().strftime('%d %b %Y, %I:%M %p'))
                    .replace('__COUNT__', str(len(rows))))
        if not rows:
            html += '<p><i>No records found for the selected filters.</i></p>'
        else:
            html += '<table><tr>' + ''.join(f'<th>{h}</th>' for h in header) + '</tr>'
            for r in rows:
                html += '<tr>' + ''.join(f'<td>{str(c)}</td>' for c in r) + '</tr>'
            html += '</table>'
        html += '<script>window.onload=function(){window.print();}</script></body></html>'
        return HttpResponse(html)

    # Excel
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Fleet Records'
    last_col = len(header)
    last_letter = openpyxl.utils.get_column_letter(max(last_col, 1))
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 24
    if last_col > 2:
        prev_letter = openpyxl.utils.get_column_letter(last_col - 1)
        ws.merge_cells(f'A1:{prev_letter}1')
        ws.merge_cells(f'A2:{prev_letter}2')
        ws.merge_cells(f'{last_letter}1:{last_letter}2')
        logo = _tyre_logo_image(width=52, height=52)
        if logo is not None:
            ws.add_image(logo, f'{last_letter}1')
    else:
        ws.merge_cells(f'A1:{last_letter}1')
        ws.merge_cells(f'A2:{last_letter}2')
    ws['A1'] = 'FLEET RECORDS'
    ws['A1'].font = Font(bold=True, size=13)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws['A2'] = f'{scope} | Generated on {date_str} | {len(rows)} record(s)'
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    header_fill = PatternFill('solid', start_color='2563EB')
    for ci, h in enumerate(header, 1):
        cell = ws.cell(row=3, column=ci, value=h)
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF')
        cell.alignment = Alignment(horizontal='center')
    thin = Side(style='thin', color='CBD5E1')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for ri, r in enumerate(rows, start=4):
        for ci, val in enumerate(r, start=1):
            ws.cell(row=ri, column=ci, value=val)
    for cell in ws[3]:
        cell.border = border
    widths = [14, 12, 10, 22, 24, 24, 30, 18]
    for ci, w in enumerate(widths[:len(header)], start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = w
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Fleet_Records_{date_str}.xlsx'
    wb.save(response)
    return response


@login_required
def add_vehicle_movement(request):
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')
    if request.method == 'POST':
        try:
            data = _movement_form_data(request)
            movement = VehicleMovement.objects.create(**data, entered_by=request.user)
            _sync_driver_shift(movement, request.user)
            messages.success(request, 'Vehicle movement saved. The complete trip history has been retained.')
        except Exception as exc:
            messages.error(request, f'Unable to save movement: {exc}')
    return redirect('fleet:vehicle_movements')


@login_required
def add_fleet_vehicle(request):
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')
    if request.method == 'POST':
        dno = (request.POST.get('dno') or '').strip()
        regn = (request.POST.get('regn') or '').strip()
        model_name = (request.POST.get('model_name') or '').strip()
        if not dno and not regn:
            messages.error(request, 'Vehicle number is required.')
        else:
            try:
                vehicle = FleetVehicle.objects.create(
                    dno=dno or regn,
                    regn=regn or None,
                    model_name=model_name or None,
                    is_active=True,
                )
                messages.success(request, f'Vehicle "{vehicle.dno}" added successfully.')
            except Exception as exc:
                messages.error(request, f'Unable to add vehicle: {exc}')
    return redirect('fleet:vehicle_movements')


@login_required
def edit_vehicle_movement(request, movement_id):
    if not _can_access_vehicle_movements(request.user):
        return redirect('dashboard')
    movement = get_object_or_404(VehicleMovement, id=movement_id)
    if request.method == 'POST':
        try:
            for field, value in _movement_form_data(request).items():
                setattr(movement, field, value)
            movement.save()
            _sync_driver_shift(movement, request.user)
            messages.success(request, 'Vehicle movement updated successfully.')
        except Exception as exc:
            messages.error(request, f'Unable to update movement: {exc}')
    return redirect('fleet:vehicle_movements')

@login_required
def vehicle_kundali(request, vehicle_id):
    from portal.models import DailyDeployment, DailyVehicleAllocation, VehicleDocument, InsuranceDocument, Employee
    from fleet.models import LubricationLog, SparePartTransaction, TyreLog, VehicleMovement, ServiceLog, SparePart
    from django.db.models import Q
    import datetime

    vehicle = get_object_or_404(FleetVehicle, id=vehicle_id)

    # Date Range Filter Parameters
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()

    df = None
    dt = None
    if date_from_str:
        try: df = datetime.datetime.strptime(date_from_str, '%Y-%m-%d').date()
        except ValueError: pass
    if date_to_str:
        try: dt = datetime.datetime.strptime(date_to_str, '%Y-%m-%d').date()
        except ValueError: pass

    logs = vehicle.repair_logs.all().order_by('-in_date')
    services = vehicle.service_logs.all().order_by('-last_date')
    
    # 1. Fetch related data across all portal modules
    lubricants = LubricationLog.objects.filter(vehicle=vehicle).order_by('-date')
    spares = SparePartTransaction.objects.filter(vehicle=vehicle).select_related('part').order_by('-date')
    tyres = TyreLog.objects.filter(vehicle=vehicle).order_by('-date')
    
    movements = VehicleMovement.objects.filter(
        Q(vehicle=vehicle) |
        (Q(vehicle_number__iexact=vehicle.regn) if vehicle.regn else Q(pk__in=[])) |
        (Q(vehicle_number__iexact=vehicle.dno) if vehicle.dno else Q(pk__in=[]))
    ).order_by('-movement_date', '-movement_time')
    
    dno_clean = vehicle.dno.strip().lower() if vehicle.dno else ""
    regn_clean = vehicle.regn.strip().lower() if vehicle.regn else ""
    deployments = DailyDeployment.objects.filter(
        Q(machinery__iexact=dno_clean) | Q(machinery__iexact=regn_clean)
    ).order_by('-date')

    allocations = DailyVehicleAllocation.objects.filter(
        Q(vehicle=vehicle) |
        (Q(vehicle_regn__iexact=vehicle.regn) if vehicle.regn else Q(pk__in=[])) |
        (Q(vehicle_regn__iexact=vehicle.dno) if vehicle.dno else Q(pk__in=[]))
    ).order_by('-date')

    # Apply date range filtering if provided
    if df:
        logs = logs.filter(in_date__gte=df)
        lubricants = lubricants.filter(date__gte=df)
        spares = spares.filter(date__gte=df)
        tyres = tyres.filter(date__gte=df)
        movements = movements.filter(movement_date__gte=df)
        deployments = deployments.filter(date__gte=df)
        allocations = allocations.filter(date__gte=df)
    if dt:
        logs = logs.filter(in_date__lte=dt)
        lubricants = lubricants.filter(date__lte=dt)
        spares = spares.filter(date__lte=dt)
        tyres = tyres.filter(date__lte=dt)
        movements = movements.filter(movement_date__lte=dt)
        deployments = deployments.filter(date__lte=dt)
        allocations = allocations.filter(date__lte=dt)

    vdocs = VehicleDocument.objects.filter(
        (Q(registration_no__iexact=vehicle.regn) if vehicle.regn else Q(pk__in=[])) |
        (Q(registration_no__iexact=vehicle.dno) if vehicle.dno else Q(pk__in=[]))
    ).order_by('-created_at')

    ins_docs = InsuranceDocument.objects.filter(
        (Q(registration_no__iexact=vehicle.regn) if vehicle.regn else Q(pk__in=[])) |
        (Q(registration_no__iexact=vehicle.dno) if vehicle.dno else Q(pk__in=[]))
    ).order_by('-expiry_date')

    assigned_employees = Employee.objects.filter(assigned_vehicle=vehicle)
    
    active_count = FleetVehicle.objects.filter(is_active=True).count()
    total_count = FleetVehicle.objects.count()
    parts_history = []
                
    for log in logs:
        if log.parts_used and str(log.parts_used).strip().lower() != 'none':
            parts_history.append({'date': log.in_date, 'source': 'Garage', 'parts': log.parts_used})
            
    def normalize_date(d):
        if isinstance(d, datetime.datetime):
            return d.date()
        return d if d else datetime.date.min
    parts_history = sorted(parts_history, key=lambda x: normalize_date(x['date']), reverse=True)
    
    last_services = vehicle.service_logs.all().order_by('-last_date')[:2]

    # Calculate metrics
    completed_repairs = logs.filter(out_date__isnull=False).count()
    active_breakdowns = logs.filter(out_date__isnull=True).count()
    
    total_shop_days = 0
    today = datetime.date.today()
    for log in logs:
        if log.in_date:
            out_d = log.out_date if log.out_date else today
            total_shop_days += (out_d - log.in_date).days

    first_record_date = logs.order_by('in_date').first().in_date if logs.exists() else None

    # Calculate breakdown frequency
    from collections import Counter
    complaints_counter = Counter([l.complaint for l in logs if l.complaint])
    total_issues = sum(complaints_counter.values())
    breakdown_freq = []
    if total_issues > 0:
        for comp, cnt in complaints_counter.most_common(5):
            perc = int((cnt / total_issues) * 100)
            breakdown_freq.append({'issue': comp, 'percentage': perc, 'count': cnt})
            
    # HMR Utilization
    hmr_per_day = 0
    if first_record_date and vehicle.latest_hmr > 0:
        days_active = (today - first_record_date).days
        if days_active > 0:
            hmr_per_day = round(vehicle.latest_hmr / days_active, 1)

    # Build unified chronological timeline across all modules
    timeline = []
    
    # 1. Repairs
    for r in logs:
        timeline.append({
            'date': r.in_date,
            'type': 'Repair',
            'icon': 'fa-wrench',
            'color': '#ef4444',
            'title': f"🔧 Repair: {r.complaint or 'Breakdown Logged'}",
            'desc': f"Mechanic: {r.mechanic or '-'} | Parts Used: {r.parts_used or 'None'}",
            'location': 'Garage Workshop',
            'extra': f"In: {r.in_date} {r.in_time or ''} | Out: {r.out_date or 'Ongoing Breakdown'} {r.out_time or ''}"
        })

    # 2. Services
    for s in services:
        timeline.append({
            'date': s.last_date,
            'type': 'Service',
            'icon': 'fa-tools',
            'color': '#0284c7',
            'title': f"⚙️ Periodic Service: {s.interval}",
            'desc': f"Current HMR: {s.current_hmr or '-'} | Next Due: {s.next_due or '-'} | Status: {s.status or 'Completed'}",
            'location': 'Garage Workshop',
            'extra': f"Remarks: {s.remarks or '-'}"
        })
        
    # 3. Lubrication
    for l in lubricants:
        timeline.append({
            'date': l.date,
            'type': 'Lubrication',
            'icon': 'fa-oil-can',
            'color': '#8b5cf6',
            'title': f"🛢️ Lubricants Consumed: {l.oil_type}",
            'desc': f"Qty: {l.qty} {l.unit} | Cost: Nu. {l.amount} | Manpower: Nu. {l.manpower_cost or 0}",
            'location': l.location or 'Gelephu',
            'extra': f"Vendor: {l.vendor or '-'} | WO No: {l.work_order_no or '-'}"
        })
        
    # 4. Spare Parts
    for s in spares:
        tx_type_str = "Received (Stock IN)" if s.transaction_type == 'IN' else "Issued (Stock OUT)"
        timeline.append({
            'date': s.date,
            'type': 'Spare Part',
            'icon': 'fa-cog',
            'color': '#3b82f6',
            'title': f"⚙️ Spare Part ({tx_type_str}): {s.part.part_name if s.part else 'Part'}",
            'desc': f"Part No: {s.part.part_number if s.part else '-'} | Qty: {s.quantity} | Total Cost: Nu. {s.total_amount}",
            'location': s.location or 'Gelephu',
            'extra': f"Supplier/WO No: {s.supplier or '-'} / {s.wo_no or '-'} | Status: {'✓ Approved' if s.is_approved else '⏳ Pending'}"
        })
        
    # 5. Tyres
    for t in tyres:
        if t.entry_type == 'ISSUE':
            timeline.append({
                'date': t.date,
                'type': 'Tyre',
                'icon': 'fa-compact-disc',
                'color': '#10b981',
                'title': f"🆕 New Tyre Issued: {t.tyre_number or 'No Number'}",
                'desc': f"Brand: {t.company or '-'} | Size: {t.size_of_tyre or '-'} | Ply: {t.ply_number or '-'} | Cost: Nu. {t.total_amount}",
                'location': t.location or 'Gelephu',
                'extra': f"Driver: {t.driver_name or '-'} | WO No: {t.work_order_no or '-'}"
            })
        else:
            timeline.append({
                'date': t.date,
                'type': 'Tyre',
                'icon': 'fa-circle-notch',
                'color': '#f59e0b',
                'title': f"🚗 Tyre Service: Punctures Logged ({t.punctures})",
                'desc': f"Material Cost: Nu. {t.material_cost} | Total Amount: Nu. {t.total_amount}",
                'location': t.location or 'Gelephu',
                'extra': f"Big/Small Patches: {t.big_patches}/{t.small_patches} | Vendor: {t.vendor or '-'}"
            })
        
    # 6. Movements
    for m in movements:
        timeline.append({
            'date': m.movement_date,
            'type': 'Movement',
            'icon': 'fa-route',
            'color': '#059669',
            'title': f"🚚 Movement to {m.destination or 'Site'}",
            'desc': f"Driver: {m.driver_name or (m.driver.name if m.driver else '-')} | Purpose: {m.purpose or '-'}",
            'location': m.destination or 'Site',
            'extra': f"Shift: {m.shift} | Time: {m.movement_time or ''}"
        })
        
    # 7. Daily Site Deployments
    for d in deployments:
        timeline.append({
            'date': d.date,
            'type': 'Deployment',
            'icon': 'fa-map-marker-alt',
            'color': '#6366f1',
            'title': "🏗️ Daily Site Deployment",
            'desc': (
                f"Zone 1-2 (D/N): {d.zone_1_2_day}/{d.zone_1_2_night} | "
                f"Zone 3-4 (D/N): {d.zone_3_4_day}/{d.zone_3_4_night} | "
                f"Culvert Area (D/N): {d.culvert_area_day}/{d.culvert_area_night}"
            ),
            'location': 'Site',
            'extra': f"Batching/Crushing Plant: {d.batching_plant_day or 0}/{d.crushing_plant_day or 0}"
        })

    # 8. Daily Shift Allocations
    for a in allocations:
        timeline.append({
            'date': a.date,
            'type': 'Allocation',
            'icon': 'fa-calendar-check',
            'color': '#2563eb',
            'title': f"📋 Shift Allocation: {a.location_zone} ({a.shift} Shift)",
            'desc': f"Driver: {a.driver_name or '-'} (ID: {a.driver_emp_id or '-'}) | WO: {a.work_order_no or '-'}",
            'location': a.location_zone,
            'extra': f"Vendor: {a.vendor or '-'} | Out: {a.out_time or '-'} In: {a.in_time or '-'}"
        })

    # 9. RC Documents
    for vd in vdocs:
        timeline.append({
            'date': vd.rc_issued_on or (vd.created_at.date() if vd.created_at else None),
            'type': 'Document',
            'icon': 'fa-id-card',
            'color': '#14b8a6',
            'title': "📄 Vehicle RC Record",
            'desc': f"Driver/Operator: {vd.driver_operator or '-'} | Type: {vd.vehicle_type or '-'}",
            'location': 'Fleet Office',
            'extra': f"RC Issued: {vd.rc_issued_on or '-'} | Expiry: {vd.rc_expiry_date or '-'}"
        })

    # 10. Insurance
    for idoc in ins_docs:
        timeline.append({
            'date': idoc.issued_on or (idoc.created_at.date() if idoc.created_at else None),
            'type': 'Insurance',
            'icon': 'fa-shield-halved',
            'color': '#84cc16',
            'title': f"🛡️ Insurance Policy: {idoc.policy_no or 'Policy Active'}",
            'desc': f"Provider: {idoc.insurance_provider or '-'} | Operator: {idoc.driver_operator or '-'}",
            'location': idoc.work_site or 'Site',
            'extra': f"Valid until: {idoc.expiry_date or '-'} | Supplier: {idoc.company_supplier or '-'}"
        })

    # Sort unified timeline chronologically descending
    timeline = sorted(timeline, key=lambda x: normalize_date(x['date']), reverse=True)

    own_val = (vehicle.extra_data.get('ownership') or '').strip().lower()
    is_hired = (own_val == 'hired')

    return render(request, 'fleet/kundali.html', {
        'vehicle': vehicle,
        'is_hired': is_hired,
        'ownership_label': 'Hired' if is_hired else 'In-House',
        'date_from': date_from_str,
        'date_to': date_to_str,
        'logs': logs,
        'services': services,
        'lubricants': lubricants,
        'spares': spares,
        'tyres': tyres,
        'movements': movements,
        'deployments': deployments,
        'allocations': allocations,
        'vdocs': vdocs,
        'ins_docs': ins_docs,
        'assigned_employees': assigned_employees,
        'timeline': timeline,
        'active_count': active_count,
        'total_count': total_count,
        'last_services': last_services,
        'parts_history': parts_history,
        'completed_repairs': completed_repairs,
        'active_breakdowns': active_breakdowns,
        'total_shop_days': total_shop_days,
        'breakdown_freq': breakdown_freq,
        'hmr_per_day': hmr_per_day,
        'all_vehicles': FleetVehicle.objects.all().order_by('dno', 'regn'),
        'common_spare_parts': list(SparePart.objects.values_list('part_name', flat=True).distinct()[:120]),
        'common_mechanics': list(RepairLog.objects.exclude(mechanic__isnull=True).exclude(mechanic='').values_list('mechanic', flat=True).distinct()[:50]),
    })


@login_required
def vehicle_profiles_hub(request):
    """Central Vehicle Profile Directory: displays all 534 fleet vehicles with real-time
    filters for In-House vs Hired, Breakdown status, instant search, Card & List views,
    interactive visual analytics, and advanced lifecycle export."""
    if not _can_access_vehicle_profiles(request.user):
        messages.error(request, "Permission denied: You do not have permission to view Vehicle Profiles.")
        return redirect('dashboard')

    import json
    import datetime
    from collections import Counter
    from fleet.models import FleetVehicle, RepairLog, LubricationLog, SparePartTransaction, TyreLog, VehicleMovement, SparePart
    from portal.models import DailyVehicleAllocation, DailyDeployment
    from django.db.models import Count, Q

    # Date Range Filter Parameters
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()

    df = None
    dt = None
    if date_from_str:
        try: df = datetime.datetime.strptime(date_from_str, '%Y-%m-%d').date()
        except ValueError: pass
    if date_to_str:
        try: dt = datetime.datetime.strptime(date_to_str, '%Y-%m-%d').date()
        except ValueError: pass

    vehicles = FleetVehicle.objects.all().select_related('driver', 'department').order_by('dno', 'regn')
    
    # Active breakdown vehicle mapping (ongoing repairs where out_date is null)
    active_repairs_qs = RepairLog.objects.filter(out_date__isnull=True).select_related('vehicle')
    if df: active_repairs_qs = active_repairs_qs.filter(in_date__gte=df)
    if dt: active_repairs_qs = active_repairs_qs.filter(in_date__lte=dt)
    ongoing_repairs_map = {}
    for r in active_repairs_qs:
        if r.vehicle_id not in ongoing_repairs_map:
            ongoing_repairs_map[r.vehicle_id] = r

    # Map latest repair log per vehicle for unified lifecycle display
    latest_repairs_map = {}
    for r in RepairLog.objects.all().order_by('in_date', 'id'):
        latest_repairs_map[r.vehicle_id] = r
    
    # Pre-aggregate counts per vehicle for instant page rendering
    repair_counts = dict(RepairLog.objects.values('vehicle_id').annotate(c=Count('id')).values_list('vehicle_id', 'c'))
    lube_counts = dict(LubricationLog.objects.values('vehicle_id').annotate(c=Count('id')).values_list('vehicle_id', 'c'))
    spare_counts = dict(SparePartTransaction.objects.values('vehicle_id').annotate(c=Count('id')).values_list('vehicle_id', 'c'))
    tyre_counts = dict(TyreLog.objects.values('vehicle_id').annotate(c=Count('id')).values_list('vehicle_id', 'c'))
    movement_counts = dict(VehicleMovement.objects.filter(vehicle__isnull=False).values('vehicle_id').annotate(c=Count('id')).values_list('vehicle_id', 'c'))
    allocation_counts = dict(DailyVehicleAllocation.objects.filter(vehicle__isnull=False).values('vehicle_id').annotate(c=Count('id')).values_list('vehicle_id', 'c'))

    hired_total = 0
    in_house_total = 0
    workshop_total = len(ongoing_repairs_map)
    today = datetime.date.today()
    
    vehicle_cards = []
    for v in vehicles:
        own_raw = (v.extra_data.get('ownership') or '').strip().lower()
        is_hired = (own_raw == 'hired')
        if is_hired:
            hired_total += 1
            ownership_label = 'Hired'
        else:
            in_house_total += 1
            ownership_label = 'In-House'

        active_repair = ongoing_repairs_map.get(v.id)
        in_workshop = (active_repair is not None)
        active_repair_id = active_repair.id if active_repair else 0
        active_complaint = (active_repair.complaint or '') if active_repair else ''
        active_mechanic = (active_repair.mechanic or '') if active_repair else ''
        active_garage = (active_repair.extra_data.get('garage_name') or 'Main Workshop') if (active_repair and isinstance(active_repair.extra_data, dict)) else 'Main Workshop'
        active_in_date = active_repair.in_date.strftime('%d-%m-%Y') if (active_repair and active_repair.in_date) else ''

        # Latest Breakdown Lifecycle Info
        latest_rep = latest_repairs_map.get(v.id)
        if latest_rep:
            latest_bd_in = latest_rep.in_date.strftime('%d-%m-%Y') if latest_rep.in_date else '-'
            latest_bd_out = latest_rep.out_date.strftime('%d-%m-%Y') if latest_rep.out_date else ('In Shop' if in_workshop else '-')
            if latest_rep.out_date and latest_rep.in_date:
                latest_downtime = f"{max(1, (latest_rep.out_date - latest_rep.in_date).days)}d"
            elif latest_rep.in_date:
                latest_downtime = f"{(today - latest_rep.in_date).days}d (in shop)"
            else:
                latest_downtime = '-'
            latest_complaint = latest_rep.complaint or '-'
            latest_garage = (latest_rep.extra_data.get('garage_name') if isinstance(latest_rep.extra_data, dict) else None) or 'Main Yard Workshop'
            latest_parts = latest_rep.parts_used or '-'
        else:
            latest_bd_in = '-'
            latest_bd_out = '-'
            latest_downtime = '-'
            latest_complaint = '-'
            latest_garage = '-'
            latest_parts = '-'

        rep_c = repair_counts.get(v.id, 0)
        lub_c = lube_counts.get(v.id, 0)
        sp_c = spare_counts.get(v.id, 0)
        ty_c = tyre_counts.get(v.id, 0)
        mv_c = movement_counts.get(v.id, 0)
        al_c = allocation_counts.get(v.id, 0)
        total_activities = rep_c + lub_c + sp_c + ty_c + mv_c + al_c

        vehicle_cards.append({
            'id': v.id,
            'dno': v.dno or '',
            'regn': v.regn or '',
            'model_name': v.model_name or '',
            'department': v.department.name if v.department else 'General Fleet',
            'driver_name': v.driver.name if v.driver else (v.extra_data.get('driver_name') or 'Not Assigned'),
            'owner_name': v.extra_data.get('owner_name') or ('Company Owned' if not is_hired else 'Hired Equipment'),
            'ownership': ownership_label,
            'is_hired': is_hired,
            'in_workshop': in_workshop,
            'active_repair_id': active_repair_id,
            'active_complaint': active_complaint,
            'active_mechanic': active_mechanic,
            'active_garage': active_garage,
            'active_in_date': active_in_date,
            'latest_bd_in': latest_bd_in,
            'latest_bd_out': latest_bd_out,
            'latest_downtime': latest_downtime,
            'latest_complaint': latest_complaint,
            'latest_garage': latest_garage,
            'latest_parts': latest_parts,
            'status_label': 'In Workshop' if in_workshop else 'Running',
            'status_class': 'badge-workshop' if in_workshop else 'badge-running',
            'latest_hmr': v.latest_hmr or 0,
            'repair_count': rep_c,
            'lube_count': lub_c,
            'spare_count': sp_c,
            'tyre_count': ty_c,
            'movement_count': mv_c,
            'allocation_count': al_c,
            'total_activities': total_activities,
        })

    running_total = max(0, len(vehicle_cards) - workshop_total)
    total_fleet = max(1, len(vehicle_cards))
    availability_rate = round((running_total / total_fleet) * 100, 1)
    breakdown_rate = round((workshop_total / total_fleet) * 100, 1)

    # 1. Fleet Problem Area Analytics
    categories = {
        'Engine & Cooling': ['engine', 'coolant', 'radiator', 'heating', 'smoke', 'oil leak', 'starter', 'alternator', 'battery', 'filter'],
        'Hydraulic System': ['hydraulic', 'hose', 'cylinder', 'jack', 'pump', 'pressure', 'seal', 'oil seal'],
        'Transmission & Clutch': ['clutch', 'gear', 'transmission', 'differential', 'propeller', 'shaft'],
        'Brakes & Air System': ['brake', 'air leak', 'booster', 'valve', 'compressor'],
        'Tyres & Suspension': ['tyre', 'tire', 'puncture', 'spring', 'leaf', 'suspension', 'axle', 'rim'],
        'Electrical & Sensor': ['wiring', 'sensor', 'light', 'indicator', 'horn', 'fuse', 'display', 'switch'],
        'Body & Welding': ['bucket', 'body', 'cabin', 'blade', 'welding', 'cracked', 'dent', 'frame'],
    }
    cat_counts = Counter()
    total_complaints = 0
    for comp in RepairLog.objects.exclude(complaint__isnull=True).exclude(complaint='').values_list('complaint', flat=True):
        c_lower = comp.lower()
        matched = False
        for cat_name, kws in categories.items():
            if any(k in c_lower for k in kws):
                cat_counts[cat_name] += 1
                matched = True
                break
        if not matched:
            cat_counts['General Maintenance'] += 1
        total_complaints += 1

    problem_areas = [
        {'category': cat, 'count': cnt, 'percent': round((cnt / max(1, total_complaints)) * 100, 1)}
        for cat, cnt in cat_counts.most_common(7)
    ]

    # 2. Monthly Trend (Past 6 Months)
    months = []
    monthly_in = []
    monthly_out = []
    for i in range(5, -1, -1):
        m_year = today.year
        m_month = today.month - i
        while m_month <= 0:
            m_month += 12
            m_year -= 1
        m_date = datetime.date(m_year, m_month, 1)
        months.append(m_date.strftime('%b %Y'))
        next_m_date = datetime.date(m_year + 1, 1, 1) if m_month == 12 else datetime.date(m_year, m_month + 1, 1)
        in_count = RepairLog.objects.filter(in_date__gte=m_date, in_date__lt=next_m_date).count()
        out_count = RepairLog.objects.filter(out_date__gte=m_date, out_date__lt=next_m_date).count()
        monthly_in.append(in_count)
        monthly_out.append(out_count)

    # 3. Garage Workload Distribution
    garages_counter = Counter()
    total_downtime_sum = 0
    resolved_repairs_count = 0
    for r in RepairLog.objects.all():
        g = 'Main Yard Workshop'
        if isinstance(r.extra_data, dict) and r.extra_data.get('garage_name'):
            g = r.extra_data['garage_name']
        garages_counter[g] += 1
        if r.out_date and r.in_date:
            total_downtime_sum += max(1, (r.out_date - r.in_date).days)
            resolved_repairs_count += 1

    top_garages = garages_counter.most_common(5)
    avg_downtime = round(total_downtime_sum / max(1, resolved_repairs_count), 1)

    # 4. Chronic Breakdown Vehicles (Top 6 High-Maintenance Assets)
    top_bd_qs = list(RepairLog.objects.values('vehicle__dno', 'vehicle__regn', 'vehicle__model_name').annotate(c=Count('id')).order_by('-c')[:6])
    top_bd_labels = [
        f"{(item['vehicle__dno'] or item['vehicle__regn'] or 'Vehicle')} ({item['vehicle__model_name'] or 'Eq'})"[:18]
        for item in top_bd_qs
    ]
    top_bd_counts = [item['c'] for item in top_bd_qs]

    # 5. Fleet Machinery & Equipment Composition
    model_counts_qs = list(FleetVehicle.objects.values('model_name').annotate(c=Count('id')).order_by('-c')[:6])
    top_model_labels = [(m['model_name'] or 'General Equipment')[:20] for m in model_counts_qs]
    top_model_counts = [m['c'] for m in model_counts_qs]

    # 6. Repair Turnaround Time Distribution (MTTR)
    downtime_buckets = {'< 24 Hours': 0, '1 - 2 Days': 0, '3 - 5 Days': 0, '6 - 10 Days': 0, '> 10 Days': 0}
    same_day_count = 0
    for r in RepairLog.objects.filter(out_date__isnull=False, in_date__isnull=False):
        repair_days = max(1, (r.out_date - r.in_date).days)
        if repair_days <= 1:
            downtime_buckets['< 24 Hours'] += 1
            same_day_count += 1
        elif repair_days <= 2:
            downtime_buckets['1 - 2 Days'] += 1
        elif repair_days <= 5:
            downtime_buckets['3 - 5 Days'] += 1
        elif repair_days <= 10:
            downtime_buckets['6 - 10 Days'] += 1
        else:
            downtime_buckets['> 10 Days'] += 1
    
    same_day_pct = round((same_day_count / max(1, resolved_repairs_count)) * 100, 1)

    # 7. Major Hired Fleet Contractors / Vendors
    vendor_counter = Counter()
    for v in FleetVehicle.objects.all():
        if v.extra_data and v.extra_data.get('owner_name'):
            vendor_counter[v.extra_data['owner_name']] += 1
    top_vendors_list = vendor_counter.most_common(6)
    top_vendor_labels = [v[0][:18] for v in top_vendors_list]
    top_vendor_counts = [v[1] for v in top_vendors_list]

    # 8. Technician / Mechanic Workload & Productivity
    mech_qs = list(RepairLog.objects.exclude(mechanic__isnull=True).exclude(mechanic='').values('mechanic').annotate(c=Count('id')).order_by('-c')[:6])
    top_mech_labels = [m['mechanic'][:16] for m in mech_qs]
    top_mech_counts = [m['c'] for m in mech_qs]

    analytics = {
        'availability_rate': availability_rate,
        'breakdown_rate': breakdown_rate,
        'running_total': running_total,
        'workshop_total': workshop_total,
        'in_house_total': in_house_total,
        'hired_total': hired_total,
        'total_lifetime_repairs': RepairLog.objects.count(),
        'avg_downtime_days': avg_downtime,
        'same_day_pct': same_day_pct,
        'highest_maint_veh': top_bd_labels[0] if top_bd_labels else 'N/A',
        'highest_maint_count': top_bd_counts[0] if top_bd_counts else 0,
        
        # Chart 1: Availability Status
        # Chart 2: Ownership Ratio
        # Chart 3: Problem Areas
        'problem_areas_labels': [p['category'] for p in problem_areas],
        'problem_areas_counts': [p['count'] for p in problem_areas],
        'problem_areas_percents': [p['percent'] for p in problem_areas],
        
        # Chart 4: 6-Month Trend
        'months': months,
        'monthly_in': monthly_in,
        'monthly_out': monthly_out,
        
        # Chart 5: Chronic Breakdown Vehicles
        'top_bd_labels': top_bd_labels,
        'top_bd_counts': top_bd_counts,
        
        # Chart 6: Fleet Machinery Distribution
        'top_model_labels': top_model_labels,
        'top_model_counts': top_model_counts,
        
        # Chart 7: Repair Turnaround Time (MTTR Distribution)
        'downtime_bucket_labels': list(downtime_buckets.keys()),
        'downtime_bucket_counts': list(downtime_buckets.values()),
        
        # Chart 8: Top Hired Fleet Contractors
        'top_vendor_labels': top_vendor_labels,
        'top_vendor_counts': top_vendor_counts,
        
        # Bonus: Top Attending Technicians
        'top_mech_labels': top_mech_labels,
        'top_mech_counts': top_mech_counts,
        
        # Garages
        'garage_labels': [g[0] for g in top_garages],
        'garage_counts': [g[1] for g in top_garages],
    }

    common_spare_parts = list(SparePart.objects.values_list('part_name', flat=True).distinct()[:120])
    common_mechanics = list(RepairLog.objects.exclude(mechanic__isnull=True).exclude(mechanic='').values_list('mechanic', flat=True).distinct()[:50])

    # ── UNIFIED FLEET ACTIVITY HISTORY TIMELINE ──────────────────────────────
    # Build a merged timeline from all 5 fleet modules, limited to 500 recent records
    def _date_key(d):
        if isinstance(d, datetime.datetime):
            return d.date()
        return d if d else datetime.date.min

    activity_history = []

    # 1. Repair / Breakdown Logs
    repair_qs = RepairLog.objects.select_related('vehicle', 'logged_by').order_by('-in_date', '-id')
    if df: repair_qs = repair_qs.filter(in_date__gte=df)
    if dt: repair_qs = repair_qs.filter(in_date__lte=dt)
    for r in repair_qs[:300]:
        v = r.vehicle
        status = '🔴 In Workshop' if not r.out_date else '✅ Repaired'
        downtime = ''
        if r.out_date and r.in_date:
            downtime = f"{max(1,(r.out_date - r.in_date).days)}d"
        elif r.in_date:
            downtime = f"{(today - r.in_date).days}d (in shop)"
        garage = (r.extra_data.get('garage_name') if isinstance(r.extra_data, dict) else None) or 'Workshop'
        activity_history.append({
            'date': r.in_date,
            'type': 'Breakdown',
            'type_icon': '🔧',
            'type_color': '#ef4444',
            'vehicle': v.dno or v.regn or '-',
            'vehicle_id': v.id,
            'detail': r.complaint or 'Breakdown logged',
            'sub': f"Mechanic: {r.mechanic or '-'} | Garage: {garage} | Downtime: {downtime or '-'}",
            'status': status,
            'amount': '',
            'logged_by': str(r.logged_by) if r.logged_by else '-',
        })

    # 2. Lubrication Logs
    lube_qs = LubricationLog.objects.filter(is_deleted=False).select_related('vehicle', 'entered_by').order_by('-date', '-id')
    if df: lube_qs = lube_qs.filter(date__gte=df)
    if dt: lube_qs = lube_qs.filter(date__lte=dt)
    for l in lube_qs[:200]:
        v = l.vehicle
        activity_history.append({
            'date': l.date,
            'type': 'Lubrication',
            'type_icon': '🛢️',
            'type_color': '#8b5cf6',
            'vehicle': v.dno or v.regn or '-',
            'vehicle_id': v.id,
            'detail': f"{l.oil_type} — {l.qty} {l.unit}",
            'sub': f"Vendor: {l.vendor or '-'} | Location: {l.location or '-'}",
            'status': '✅ Done',
            'amount': f"Nu. {l.total_amount}" if l.total_amount else '',
            'logged_by': str(l.entered_by) if l.entered_by else '-',
        })

    # 3. Tyre Logs
    tyre_qs = TyreLog.objects.filter(is_deleted=False).select_related('vehicle', 'entered_by').order_by('-date', '-id')
    if df: tyre_qs = tyre_qs.filter(date__gte=df)
    if dt: tyre_qs = tyre_qs.filter(date__lte=dt)
    for t in tyre_qs[:150]:
        v = t.vehicle
        if t.entry_type == 'ISSUE':
            detail = f"Tyre Issued: {t.tyre_number or '-'} ({t.company or '-'}, {t.size_of_tyre or '-'})"
        else:
            detail = f"Tyre Repair: {t.punctures}x Puncture, {t.big_patches}x Big, {t.small_patches}x Small"
        activity_history.append({
            'date': t.date,
            'type': 'Tyre',
            'type_icon': '🚗',
            'type_color': '#10b981',
            'vehicle': v.dno or v.regn or '-',
            'vehicle_id': v.id,
            'detail': detail,
            'sub': f"Location: {t.location or '-'} | Driver: {t.driver_name or '-'}",
            'status': '✅ Done',
            'amount': f"Nu. {t.total_amount}" if t.total_amount else '',
            'logged_by': str(t.entered_by) if t.entered_by else '-',
        })

    # 4. Spare Parts (Stock Out to Vehicle)
    spare_qs = SparePartTransaction.objects.filter(
        is_deleted=False, transaction_type='OUT', vehicle__isnull=False
    ).select_related('vehicle', 'part', 'entered_by').order_by('-date', '-id')
    if df: spare_qs = spare_qs.filter(date__gte=df)
    if dt: spare_qs = spare_qs.filter(date__lte=dt)
    for s in spare_qs[:200]:
        v = s.vehicle
        activity_history.append({
            'date': s.date,
            'type': 'Spare Part',
            'type_icon': '⚙️',
            'type_color': '#3b82f6',
            'vehicle': v.dno or v.regn or '-',
            'vehicle_id': v.id,
            'detail': f"{s.part.part_name if s.part else '-'} × {s.quantity} {s.part.unit if s.part else ''}",
            'sub': f"WO: {s.wo_no or s.reference_no or '-'} | Mechanic: {s.mechanic or '-'}",
            'status': '✅ Issued',
            'amount': f"Nu. {s.total_amount}" if s.total_amount else '',
            'logged_by': str(s.entered_by) if s.entered_by else '-',
        })

    # 5. Vehicle Movements
    move_qs = VehicleMovement.objects.filter(vehicle__isnull=False).select_related('vehicle').order_by('-movement_date', '-id')
    if df: move_qs = move_qs.filter(movement_date__gte=df)
    if dt: move_qs = move_qs.filter(movement_date__lte=dt)
    for m in move_qs[:150]:
        v = m.vehicle
        activity_history.append({
            'date': m.movement_date,
            'type': 'Movement',
            'type_icon': '🚚',
            'type_color': '#059669',
            'vehicle': v.dno or v.regn or '-',
            'vehicle_id': v.id,
            'detail': f"Gate Movement — Destination: {m.destination or '-'} | Purpose: {m.purpose or '-'}",
            'sub': f"Driver: {m.driver_name or '-'} | Shift: {m.shift or '-'} | Time: {m.movement_time or '-'}",
            'status': '✅ Logged',
            'amount': '',
            'logged_by': str(m.entered_by) if m.entered_by else '-',
        })

    # Sort descending by date
    activity_history.sort(key=lambda x: _date_key(x['date']), reverse=True)
    # Cap at 600 entries for template rendering performance
    activity_history = activity_history[:600]

    context = {
        'vehicles': vehicle_cards,
        'vehicles_json': json.dumps(vehicle_cards),
        'all_vehicles': vehicles,
        'total_count': len(vehicle_cards),
        'hired_total': hired_total,
        'in_house_total': in_house_total,
        'workshop_total': workshop_total,
        'running_total': running_total,
        'date_from': date_from_str,
        'date_to': date_to_str,
        'activity_history': activity_history,
        'activity_total': len(activity_history),
        'common_spare_parts': common_spare_parts,
        'common_mechanics': common_mechanics,
        'analytics': analytics,
        'analytics_json': json.dumps(analytics),
        'can_log_breakdown': _can_access_breakdown_register(request.user),
    }
    return render(request, 'fleet/vehicle_profiles.html', context)


@login_required
def api_spare_parts_search(request):
    """Live search API for spare parts matching query parameter 'q'."""
    from fleet.models import SparePart
    from django.db.models import Q
    q = request.GET.get('q', '').strip()
    qs = SparePart.objects.all()
    if q:
        qs = qs.filter(Q(part_name__icontains=q) | Q(part_number__icontains=q))
    parts = [
        {
            'id': p['id'],
            'part_number': (p['part_number'] or '').strip(),
            'part_name': (p['part_name'] or '').strip(),
            'unit': (p['unit'] or '').strip(),
        }
        for p in qs.values('id', 'part_number', 'part_name', 'unit')[:40]
    ]
    return JsonResponse({'success': True, 'parts': parts})


@login_required
def api_add_breakdown(request):
    """API to log a new vehicle breakdown or repair ticket. All fields are optional."""
    from fleet.models import FleetVehicle, RepairLog, get_or_create_fleet_vehicle
    import datetime

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    if not _can_access_breakdown_register(request.user):
        return JsonResponse({'success': False, 'message': 'Permission denied: You do not have permission to log breakdowns.'}, status=403)

    try:
        vehicle_id = request.POST.get('vehicle_id')
        vehicle_custom = (request.POST.get('vehicle_custom') or '').strip()
        vehicle = None
        if vehicle_id:
            try:
                vehicle = FleetVehicle.objects.get(id=vehicle_id)
            except (FleetVehicle.DoesNotExist, ValueError):
                vehicle = None

        if not vehicle and vehicle_custom:
            vehicle = get_or_create_fleet_vehicle(vehicle_custom)

        if not vehicle:
            vehicle = FleetVehicle.objects.first()

        in_date_str = request.POST.get('in_date')
        in_date = datetime.datetime.strptime(in_date_str, '%Y-%m-%d').date() if in_date_str else datetime.date.today()

        in_time_str = request.POST.get('in_time')
        in_time = None
        if in_time_str:
            try:
                in_time = datetime.datetime.strptime(in_time_str, '%H:%M').time()
            except ValueError:
                pass

        complaint = request.POST.get('complaint', '').strip() or 'General Inspection / Breakdown'
        garage_name = request.POST.get('garage_name', '').strip() or 'Main Yard Workshop'
        breakdown_location = request.POST.get('breakdown_location', '').strip()
        mechanic = request.POST.get('mechanic', '').strip()

        # Handle multiple dynamic parts
        part_names = request.POST.getlist('part_names[]') or ([request.POST.get('parts_used')] if request.POST.get('parts_used') else [])
        part_qtys = request.POST.getlist('part_qtys[]') or ([request.POST.get('qty')] if request.POST.get('qty') else [])

        parts_combined = []
        parts_detailed = []
        for name, q in zip(part_names, part_qtys):
            name = (name or '').strip()
            q = (q or '').strip()
            if name:
                parts_detailed.append({'part_name': name, 'qty': q})
                if q:
                    parts_combined.append(f"{name} ({q})")
                else:
                    parts_combined.append(name)

        parts_used = ", ".join(parts_combined) if parts_combined else request.POST.get('parts_used', '').strip()
        qty = ", ".join([p['qty'] for p in parts_detailed if p.get('qty')]) if parts_detailed else request.POST.get('qty', '').strip()

        hmr_val = request.POST.get('hmr_at_breakdown')
        hmr_at_breakdown = float(hmr_val) if hmr_val else None

        kmr_val = request.POST.get('kmr_at_breakdown')
        kmr_at_breakdown = float(kmr_val) if kmr_val else None

        out_date_str = request.POST.get('out_date')
        out_date = datetime.datetime.strptime(out_date_str, '%Y-%m-%d').date() if out_date_str else None

        out_time_str = request.POST.get('out_time')
        out_time = None
        if out_time_str:
            try:
                out_time = datetime.datetime.strptime(out_time_str, '%H:%M').time()
            except ValueError:
                pass

        remarks = request.POST.get('remarks', '').strip()
        extra_data = {
            'garage_name': garage_name,
            'breakdown_location': breakdown_location,
            'parts_detailed': parts_detailed,
        }

        log = RepairLog.objects.create(
            vehicle=vehicle,
            logged_by=request.user,
            in_date=in_date,
            in_time=in_time,
            complaint=complaint,
            mechanic=mechanic,
            parts_used=parts_used,
            qty=qty,
            hmr_at_breakdown=hmr_at_breakdown,
            kmr_at_breakdown=kmr_at_breakdown,
            out_date=out_date,
            out_time=out_time,
            remarks=remarks,
            extra_data=extra_data
        )

        return JsonResponse({
            'success': True,
            'message': f'Breakdown successfully logged for {vehicle.dno or vehicle.regn if vehicle else "Vehicle"}',
            'log_id': log.id,
            'is_active': (log.out_date is None)
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
def api_resolve_breakdown(request, log_id):
    """API to complete an active breakdown repair and release the vehicle back into service. All fields optional."""
    from fleet.models import RepairLog
    import datetime

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    if not _can_access_breakdown_register(request.user):
        return JsonResponse({'success': False, 'message': 'Permission denied: You do not have permission to resolve or release vehicle repairs.'}, status=403)

    try:
        log = get_object_or_404(RepairLog, id=log_id)

        out_date_str = request.POST.get('out_date')
        out_date = datetime.datetime.strptime(out_date_str, '%Y-%m-%d').date() if out_date_str else datetime.date.today()

        out_time_str = request.POST.get('out_time')
        out_time = None
        if out_time_str:
            try:
                out_time = datetime.datetime.strptime(out_time_str, '%H:%M').time()
            except ValueError:
                pass

        # Handle multiple dynamic parts
        part_names = request.POST.getlist('part_names[]') or ([request.POST.get('parts_used')] if request.POST.get('parts_used') else [])
        part_qtys = request.POST.getlist('part_qtys[]') or ([request.POST.get('qty')] if request.POST.get('qty') else [])

        parts_combined = []
        parts_detailed = []
        for name, q in zip(part_names, part_qtys):
            name = (name or '').strip()
            q = (q or '').strip()
            if name:
                parts_detailed.append({'part_name': name, 'qty': q})
                if q:
                    parts_combined.append(f"{name} ({q})")
                else:
                    parts_combined.append(name)

        if parts_combined:
            log.parts_used = ", ".join(parts_combined)
            log.qty = ", ".join([p['qty'] for p in parts_detailed if p.get('qty')])
        elif request.POST.get('parts_used'):
            log.parts_used = request.POST.get('parts_used', '').strip()
            log.qty = request.POST.get('qty', '').strip()

        mechanic = request.POST.get('mechanic', '').strip()
        if mechanic:
            log.mechanic = mechanic

        garage_name = request.POST.get('garage_name', '').strip()
        if not isinstance(log.extra_data, dict):
            log.extra_data = {}
        if garage_name:
            log.extra_data['repaired_at_garage'] = garage_name
        if parts_detailed:
            log.extra_data['parts_detailed'] = parts_detailed

        remarks = request.POST.get('remarks', '').strip()
        if remarks:
            log.remarks = remarks

        log.out_date = out_date
        log.out_time = out_time
        log.save()

        active_count = RepairLog.objects.filter(out_date__isnull=True).count()

        return JsonResponse({
            'success': True,
            'message': f'Vehicle {log.vehicle.dno or log.vehicle.regn} marked as Repaired and Released successfully!',
            'active_breakdowns_count': active_count
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
def breakdown_register(request):
    """Workshop Breakdown Register: view all active breakdowns and completed repair history with live modal."""
    if not _can_access_breakdown_register(request.user):
        messages.error(request, "Permission denied: You do not have permission to access the Workshop Breakdown Register.")
        return redirect('dashboard')

    from fleet.models import FleetVehicle, RepairLog, SparePart

    vehicles = FleetVehicle.objects.all().order_by('dno', 'regn')
    active_repairs = RepairLog.objects.filter(out_date__isnull=True).select_related('vehicle', 'logged_by').order_by('-in_date', '-id')
    completed_repairs = RepairLog.objects.filter(out_date__isnull=False).select_related('vehicle', 'logged_by').order_by('-out_date', '-id')[:100]
    common_spare_parts = list(SparePart.objects.values_list('part_name', flat=True).distinct()[:120])
    common_mechanics = list(RepairLog.objects.exclude(mechanic__isnull=True).exclude(mechanic='').values_list('mechanic', flat=True).distinct()[:50])

    context = {
        'vehicles': vehicles,
        'active_repairs': active_repairs,
        'completed_repairs': completed_repairs,
        'active_count': active_repairs.count(),
        'completed_count': RepairLog.objects.filter(out_date__isnull=False).count(),
        'total_count': RepairLog.objects.count(),
        'common_spare_parts': common_spare_parts,
        'common_mechanics': common_mechanics,
    }
    return render(request, 'fleet/breakdowns.html', context)



@login_required
def fleet_dashboard(request, section_id=None):
    from portal.models import CompanyPost, Employee, DailyDeployment
    from fleet.models import RepairLog, VehicleMovement, LubricationLog, SparePartTransaction, TyreLog
    
    vehicles = FleetVehicle.objects.all().order_by('regn')
    departments = CompanyPost.objects.filter(id__in=FleetVehicle.objects.filter(department__isnull=False).values_list('department_id', flat=True).distinct()).order_by('name')
    
    # Optimize using in-memory mappings to avoid N+1 query problems (563+ database calls)
    active_breakdown_vehicle_ids = set(
        RepairLog.objects.filter(out_date__isnull=True).values_list('vehicle_id', flat=True)
    )
    
    # 1. Fetch day/night drivers
    all_employees = Employee.objects.filter(assigned_vehicle__isnull=False)
    drivers_by_vehicle = {}
    for emp in all_employees:
        drivers_by_vehicle.setdefault(emp.assigned_vehicle_id, []).append(emp)
        
    # 2. Fetch latest movements
    movements_list = VehicleMovement.objects.filter(is_deleted=False).order_by('vehicle_id', '-movement_date', '-id')
    latest_movements = {}
    for mv in movements_list:
        if mv.vehicle_id not in latest_movements:
            latest_movements[mv.vehicle_id] = mv

    # 3. Fetch latest lubrication logs
    lubs_list = LubricationLog.objects.filter(is_deleted=False).order_by('vehicle_id', '-date', '-id')
    latest_lubs = {}
    for lb in lubs_list:
        if lb.vehicle_id not in latest_lubs:
            latest_lubs[lb.vehicle_id] = lb

    # 4. Fetch latest spare part transactions
    spares_list = SparePartTransaction.objects.filter(is_deleted=False).order_by('vehicle_id', '-date', '-id')
    latest_spares = {}
    for sp in spares_list:
        if sp.vehicle_id not in latest_spares:
            latest_spares[sp.vehicle_id] = sp

    # 5. Fetch latest tyre logs
    tyres_list = TyreLog.objects.filter(is_deleted=False).order_by('vehicle_id', '-date', '-id')
    latest_tyres = {}
    for ty in tyres_list:
        if ty.vehicle_id not in latest_tyres:
            latest_tyres[ty.vehicle_id] = ty

    # 6. Fetch latest daily deployments
    deps_list = DailyDeployment.objects.all().order_by('-date', '-id')
    latest_deps = {}
    for dp in deps_list:
        mach = dp.machinery.strip().lower() if dp.machinery else ""
        if mach and mach not in latest_deps:
            latest_deps[mach] = dp
            
    for veh in vehicles:
        veh.status = "In Workshop" if veh.id in active_breakdown_vehicle_ids else "Running"
        
        assigned = drivers_by_vehicle.get(veh.id, [])
        veh.day_driver = next((emp for emp in assigned if emp.current_shift == 'Day'), None)
        veh.night_driver = next((emp for emp in assigned if emp.current_shift == 'Night'), None)
        
        # Attach last updates
        veh.last_movement = latest_movements.get(veh.id)
        veh.last_lubrication = latest_lubs.get(veh.id)
        veh.last_spare = latest_spares.get(veh.id)
        veh.last_tyre = latest_tyres.get(veh.id)
        
        # Match deployment
        dno_key = veh.dno.strip().lower() if veh.dno else ""
        regn_key = veh.regn.strip().lower() if veh.regn else ""
        veh.last_deployment = latest_deps.get(dno_key) or latest_deps.get(regn_key)
        
    return render(request, 'fleet/total_vehicles.html', {
        'vehicles': vehicles,
        'departments': departments
    })

@login_required
def api_add_vehicle(request):
    from django.shortcuts import redirect
    from django.contrib import messages
    from portal.models import CompanyPost
    
    if request.method == 'POST':
        try:
            dno = request.POST.get('dno')
            regn = request.POST.get('regn')
            model_name = request.POST.get('model_name')
            dept_id = request.POST.get('department_id')
            
            dept = None
            if dept_id:
                dept = CompanyPost.objects.get(id=dept_id)
                
            extra_data = {
                'ownership': request.POST.get('ownership', 'In-house'),
                'owner_name': request.POST.get('owner_name', ''),
                'contract_ref': request.POST.get('contract_ref', ''),
                'contract_status': request.POST.get('contract_status', 'Active'),
            }
                
            FleetVehicle.objects.create(
                dno=dno,
                regn=regn,
                model_name=model_name,
                department=dept,
                extra_data=extra_data
            )
            messages.success(request, f"Vehicle {dno} added successfully.")
        except Exception as e:
            messages.error(request, f"Error adding vehicle: {str(e)}")
    return redirect('fleet:global_dashboard')

@login_required
def api_edit_vehicle(request, vehicle_id):
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    from portal.models import CompanyPost
    
    vehicle = get_object_or_404(FleetVehicle, id=vehicle_id)
    if request.method == 'POST':
        try:
            vehicle.dno = request.POST.get('dno')
            vehicle.regn = request.POST.get('regn')
            vehicle.model_name = request.POST.get('model_name')
            dept_id = request.POST.get('department_id')
            
            if dept_id:
                vehicle.department = CompanyPost.objects.get(id=dept_id)
            else:
                vehicle.department = None
                
            # Update extra_data
            vehicle.extra_data = {
                'ownership': request.POST.get('ownership', 'In-house'),
                'owner_name': request.POST.get('owner_name', ''),
                'contract_ref': request.POST.get('contract_ref', ''),
                'contract_status': request.POST.get('contract_status', 'Active'),
            }
                
            vehicle.save()
            messages.success(request, f"Vehicle {vehicle.dno} updated successfully.")
        except Exception as e:
            messages.error(request, f"Error updating vehicle: {str(e)}")
    return redirect('fleet:global_dashboard')

@login_required
def api_delete_vehicle(request, vehicle_id):
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    
    vehicle = get_object_or_404(FleetVehicle, id=vehicle_id)
    if request.method == 'POST':
        try:
            name = vehicle.dno
            vehicle.delete()
            messages.success(request, f"Vehicle {name} deleted successfully.")
        except Exception as e:
            messages.error(request, f"Error deleting vehicle: {str(e)}")
    return redirect('fleet:global_dashboard')

from django.contrib.auth.decorators import login_required
from .models import LubricationLog, FleetVehicle, HiredVehicle

@login_required
def lubrication_view(request):
    logs = LubricationLog.objects.all().order_by('-date', '-id')
    vehicles = FleetVehicle.objects.all().order_by('regn')
    
    context = {
        'logs': logs,
        'vehicles': vehicles,
    }
    return render(request, 'fleet/lubrication.html', context)


# ========== LUBRICATION VIEWS ==========
from .models import LubricationLog
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

@login_required
def lubrication_view(request):
    from django.db.models import Sum, Count
    import datetime, calendar
    
    logs = LubricationLog.objects.select_related('vehicle', 'entered_by').filter(is_deleted=False).order_by('-date', '-id')
    vehicles = FleetVehicle.objects.filter(is_active=True).order_by('regn')
    
    today = datetime.date.today()
    month_start = today.replace(day=1)
    
    month_logs = LubricationLog.objects.filter(date__gte=month_start, is_deleted=False)
    stats_month = "This Month"
    
    if not month_logs.exists():
        # Fallback to the latest month that has data
        latest_log = LubricationLog.objects.filter(is_deleted=False).order_by('-date').first()
        if latest_log:
            month_start = latest_log.date.replace(day=1)
            _, last_day = calendar.monthrange(month_start.year, month_start.month)
            month_end = month_start.replace(day=last_day)
            month_logs = LubricationLog.objects.filter(date__range=(month_start, month_end), is_deleted=False)
            stats_month = month_start.strftime("%b %Y")
            
    month_amount = month_logs.aggregate(total=Sum('total_amount'))['total'] or 0
    month_qty = month_logs.aggregate(total=Sum('qty'))['total'] or 0
    
    locations = sorted(set(v for v in LubricationLog.objects.filter(is_deleted=False).values_list('location', flat=True).distinct() if v))
    oil_types = sorted(set(v for v in LubricationLog.objects.filter(is_deleted=False).values_list('oil_type', flat=True).distinct() if v))
    vendors = sorted(set(v for v in LubricationLog.objects.filter(is_deleted=False).values_list('vendor', flat=True).distinct() if v))
    work_orders = sorted(set(v for v in LubricationLog.objects.filter(is_deleted=False).values_list('work_order_no', flat=True).distinct() if v))
    hired_vehicles = list(HiredVehicle.objects.exclude(owner_name='').values('regn', 'equipment_type', 'owner_name', 'agreement_ref'))
    
    latest_log = LubricationLog.objects.filter(is_deleted=False).order_by('-id').first()
    context = {
        'logs': logs,
        'vehicles': vehicles,
        'total_entries': logs.count(),
        'month_amount': round(month_amount, 2),
        'month_qty': round(month_qty, 2),
        'stats_month': stats_month,
        'locations': locations,
        'oil_types': oil_types,
        'vendors': vendors,
        'work_orders': work_orders,
        'hired_vehicles': json.dumps(hired_vehicles),
        'latest_log_id': latest_log.id if latest_log else 0,
        'total_log_count': logs.count(),
    }
    return render(request, 'fleet/lubrication.html', context)


@login_required
@require_POST
def api_add_lubrication(request):
    """Add a lubrication entry. All fields are optional."""
    import datetime
    from decimal import Decimal
    from .models import get_or_create_fleet_vehicle, FleetVehicle
    try:
        vehicle_input = (request.POST.get('vehicle_regn') or request.POST.get('vehicle_id') or '').strip()
        date_str = request.POST.get('date')
        location = (request.POST.get('location') or '').strip()
        work_order_no = (request.POST.get('work_order_no') or '').strip()
        vendor = (request.POST.get('vendor') or '').strip()
        oil_type = (request.POST.get('oil_type') or '').strip() or 'General Lubricant'
        
        try:
            qty = float(request.POST.get('qty') or 0.0)
        except (ValueError, TypeError):
            qty = 0.0
            
        unit = (request.POST.get('unit') or 'L').strip()
        
        try:
            rate = Decimal(str(request.POST.get('rate') or '0.00'))
        except Exception:
            rate = Decimal('0.00')
            
        try:
            manpower_cost = Decimal(str(request.POST.get('manpower_cost') or '0.00'))
        except Exception:
            manpower_cost = Decimal('0.00')
            
        remarks = (request.POST.get('remarks') or '').strip()
        
        amount = Decimal(str(qty)) * rate
        total_amount = amount + manpower_cost
        
        vehicle = None
        if vehicle_input:
            vehicle = get_or_create_fleet_vehicle(
                vehicle_input,
                vendor=vendor,
                work_order_no=work_order_no,
                location=location
            )
        if not vehicle:
            vehicle = FleetVehicle.objects.first()
            
        if not vendor and vehicle:
            hv = HiredVehicle.objects.filter(regn__iexact=vehicle.regn).first()
            vendor = (hv.owner_name if hv else '') or (vehicle.extra_data.get('owner_name', '') if isinstance(vehicle.extra_data, dict) else '')
        if not work_order_no and vehicle:
            hv = HiredVehicle.objects.filter(regn__iexact=vehicle.regn).first()
            work_order_no = (hv.agreement_ref if hv else '') or (vehicle.extra_data.get('contract_ref', '') if isinstance(vehicle.extra_data, dict) else '')
            
        date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.date.today()
        
        LubricationLog.objects.create(
            vehicle=vehicle,
            date=date,
            vendor=vendor,
            location=location,
            work_order_no=work_order_no,
            oil_type=oil_type,
            qty=qty,
            unit=unit,
            rate=rate,
            amount=amount,
            manpower_cost=manpower_cost,
            total_amount=total_amount,
            entered_by=request.user,
        )
        from django.contrib import messages
        messages.success(request, f'Lubrication entry for {vehicle.regn if vehicle else "Fleet"} added successfully.')
    except Exception as e:
        from django.contrib import messages
        messages.error(request, f'Error: {str(e)}')
    return redirect('/fleet/lubrication/')


@login_required
def api_import_lubrication(request):
    if request.method != 'POST':
        return redirect('/fleet/lubrication/')
    
    from decimal import Decimal
    import datetime, re
    
    excel_file = request.FILES.get('excel_file')
    if not excel_file:
        from django.contrib import messages
        messages.error(request, 'No file uploaded.')
        return redirect('/fleet/lubrication/')
    
    wb = openpyxl.load_workbook(excel_file, data_only=True)
    
    errors = []
    imported = 0
    
    def parse_qty_unit(qty_val):
        """Parse qty like '1L', '500ml', '25Kg', '10' into (float, str)"""
        if qty_val is None:
            return 0.0, 'L'
        qty_str = str(qty_val).strip()
        match = re.match(r'^([\d.]+)\s*([a-zA-Z]*)$', qty_str)
        if match:
            return float(match.group(1) or 0), match.group(2) or 'L'
        try:
            return float(qty_str), 'L'
        except:
            return 0.0, 'L'
    
    def parse_date(date_val):
        """Parse dates like '01.07.26', '2026-07-01', or datetime objects"""
        if date_val is None:
            return None
        if isinstance(date_val, datetime.datetime):
            return date_val.date()
        if isinstance(date_val, datetime.date):
            return date_val
        date_str = str(date_val).strip()
        # Try dd.mm.yy
        try:
            return datetime.datetime.strptime(date_str, '%d.%m.%y').date()
        except:
            pass
        # Try dd.mm.yyyy
        try:
            return datetime.datetime.strptime(date_str, '%d.%m.%Y').date()
        except:
            pass
        # Try yyyy-mm-dd
        try:
            return datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        except:
            pass
        return None
    
    # Process all sheets
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        vendor_name = sheet_name  # Sheet name = vendor
        
        # Find header row (SL NO in column A)
        header_row = None
        for row_idx in range(1, 10):
            cell_val = ws.cell(row=row_idx, column=1).value
            if cell_val and str(cell_val).strip().upper() in ('SL NO', 'SL. NO', 'SL.NO'):
                header_row = row_idx
                break
        
        if header_row is None:
            continue  # Skip sheets without proper header
        
        # Data starts from row after header
        # Column mapping based on the known format:
        # A=SL NO, B=DATE, C=LOCATION, D=WO NO, E=REG NO, F=PARTICULARS (cols F-I merged), J=QTY, K=RATE, L=AMOUNT, M=COST OF MAN POWER, N=TOTAL AMOUNT
        
        current_date = None
        current_location = None
        current_wo = None
        current_regn = None
        current_total_amount = None  # Total on the "main" row
        sub_items = []  # For grouped items
        
        for row_idx in range(header_row + 1, ws.max_row + 1):
            row = [ws.cell(row=row_idx, column=c).value for c in range(1, 15)]
            
            sl_no = row[0]
            date_val = row[1]
            location = row[2]
            wo_no = row[3]
            regn = row[4]
            particulars = row[5]  # Col F
            qty_raw = row[9]   # Col J
            rate_val = row[10]  # Col K
            amount_val = row[11] # Col L
            manpower_val = row[12] # Col M
            total_val = row[13]  # Col N
            
            # Skip empty rows
            if sl_no is None and date_val is None and regn is None and particulars is None:
                continue
            
            # If no reg no, it's a continuation sub-item of previous row
            if regn is None and sl_no is not None and particulars:
                pass  # treat as standalone with current_regn
            
            # Try to get vehicle regn
            if regn:
                current_regn = str(regn).strip()
            if date_val:
                current_date = parse_date(date_val)
            if location:
                current_location = str(location).strip()
            if wo_no:
                current_wo = str(wo_no).strip()
            
            if not current_regn or not particulars:
                continue
            
            qty, unit = parse_qty_unit(qty_raw)
            if qty == 0:
                continue
                
            try:
                vehicle = FleetVehicle.objects.get(regn__iexact=current_regn)
            except FleetVehicle.DoesNotExist:
                errors.append(f"Sheet '{sheet_name}' Row {row_idx}: Vehicle '{current_regn}' not found.")
                continue
            
            try:
                rate = Decimal(str(rate_val or 0))
                amount = Decimal(str(amount_val or 0)) if amount_val else Decimal(str(qty)) * rate
                manpower = Decimal(str(manpower_val or 0))
                total = Decimal(str(total_val or 0)) if total_val else amount + manpower
                
                if current_date is None:
                    errors.append(f"Sheet '{sheet_name}' Row {row_idx}: Could not parse date.")
                    continue
                
                LubricationLog.objects.create(
                    date=current_date,
                    location=current_location or '',
                    work_order_no=current_wo or '',
                    vendor=(vendor_name or '') or ((HiredVehicle.objects.filter(regn__iexact=current_regn).first().owner_name or '') if HiredVehicle.objects.filter(regn__iexact=current_regn).exists() else ''),
                    vehicle=vehicle,
                    oil_type=str(particulars).strip(),
                    qty=qty,
                    unit=unit or 'L',
                    rate=rate,
                    amount=amount,
                    manpower_cost=manpower,
                    total_amount=total,
                    entered_by=request.user,
                )
                imported += 1
            except Exception as e:
                errors.append(f"Sheet '{sheet_name}' Row {row_idx}: {str(e)}")
    
    from django.contrib import messages
    if imported:
        messages.success(request, f'{imported} entries imported successfully.' + (f' {len(errors)} errors skipped.' if errors else ''))
    else:
        messages.error(request, f'No entries imported. {len(errors)} errors found.')
    if errors and not imported:
        messages.warning(request, ' | '.join(errors[:5]))
    
    return redirect('/fleet/lubrication/')


@login_required
def download_lubrication_sample(request):
    """Generate sample Excel in the same format as the actual lubricant file"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Sample Vendor'
    
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import Border, Side
    
    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    
    # Row 1 & 2 heights
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 24
    
    # Row 1: Project name
    ws.merge_cells('A1:M1')
    ws['A1'] = 'Proposed International Airport Project in Gelephu'
    ws['A1'].font = Font(bold=True, size=12)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    
    # Row 2: Title + Vendor name
    ws.merge_cells('A2:L2')
    ws['A2'] = 'DEBIT NOTE FOR LUBRICANTS CONSUMED'
    ws['A2'].font = Font(bold=True, size=13)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws['M2'] = 'Vendor Name'
    ws['M2'].font = Font(bold=True, size=10)
    ws['M2'].alignment = Alignment(horizontal='center', vertical='center')
    
    # Logo cell at N (Rows 1-2 merged)
    ws.merge_cells('N1:N2')
    logo = _tyre_logo_image(width=52, height=52)
    if logo is not None:
        ws.add_image(logo, 'N1')
    
    # Row 3: Headers
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF', size=10)
    
    headers = {
        'A3': 'SL NO', 'B3': 'DATE', 'C3': 'LOCATION', 'D3': 'WO NO', 'E3': 'REG NO',
        'F3': 'PARTICULARS', 'J3': 'QTY', 'K3': 'RATE', 'L3': 'AMOUNT',
        'M3': 'COST OF MAN POWER', 'N3': 'TOTAL AMOUNT'
    }
    ws.merge_cells('F3:I3')
    for cell_addr, val in headers.items():
        ws[cell_addr] = val
        ws[cell_addr].fill = header_fill
        ws[cell_addr].font = header_font
        ws[cell_addr].alignment = Alignment(horizontal='center', vertical='center')
        ws[cell_addr].border = border
    
    # Column widths
    col_widths = {'A': 8, 'B': 12, 'C': 12, 'D': 22, 'E': 18, 'F': 14, 'G': 6, 'H': 6, 'I': 6, 'J': 8, 'K': 8, 'L': 10, 'M': 16, 'N': 14}
    for col, width in col_widths.items():
        ws.column_dimensions[col].width = width
    
    # Sample data rows
    sample_rows = [
        (1, '01.08.26', 'Gelephu', 'RVJ/HA/HI/26-08-001', 'CG-13BE-9188', 'Engine Oil 15W40', None, None, None, '10L', 285, 2850, None, 2850),
        (2, '01.08.26', 'Gelephu', 'RVJ/HA/HI/26-08-002', 'CG-13BA-1466', 'Gear Oil', None, None, None, '17L', 390, 6630, None, 6630),
        (3, '02.08.26', 'Gelephu', 'RVJ/HA/HI/26-08-003', 'CG-13BE-9250', 'Hydraulic Oil', None, None, None, '25L', 230, 5750, 500, 6250),
    ]
    for row_data in sample_rows:
        ws.append(row_data)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Lubrication_Sample_Format.xlsx'
    wb.save(response)
    return response


@login_required
def api_export_lubrication(request):
    """Export in same format as the actual lubricant debit note file with query filters"""
    from openpyxl.styles import Border, Side
    
    logs = LubricationLog.objects.select_related('vehicle').all().order_by('date', 'id')
    
    # Apply Filters from GET parameters
    start_date = request.GET.get('start_date') or request.GET.get('date_from')
    end_date = request.GET.get('end_date') or request.GET.get('date_to')
    vehicle_id = request.GET.get('vehicle_id')
    location = request.GET.get('location')
    oil_type = request.GET.get('oil_type')
    vendor = request.GET.get('vendor')
    work_order_q = request.GET.get('work_order_no') or request.GET.get('wo')
    
    if start_date:
        logs = logs.filter(date__gte=start_date)
    if end_date:
        logs = logs.filter(date__lte=end_date)
    if vehicle_id:
        logs = logs.filter(vehicle_id=vehicle_id)
    if location:
        logs = logs.filter(location__iexact=location)
    if oil_type:
        logs = logs.filter(oil_type__iexact=oil_type)
    if vendor:
        logs = logs.filter(vendor__iexact=vendor)
    if work_order_q:
        logs = logs.filter(work_order_no__icontains=work_order_q)
    if vendor:
        logs = logs.filter(vendor__iexact=vendor)
    if work_order_q:
        logs = logs.filter(work_order_no__icontains=work_order_q)
        
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Lubricants Consumed'
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 24
    
    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF', size=10)
    total_fill = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')
    
    # Row 1: Project name
    ws.merge_cells('A1:M1')
    ws['A1'] = 'Proposed International Airport Project in Gelephu'
    ws['A1'].font = Font(bold=True, size=12)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    
    # Row 2: Title & Vendor
    ws.merge_cells('A2:L2')
    ws['A2'] = 'DEBIT NOTE FOR LUBRICANTS CONSUMED'
    ws['A2'].font = Font(bold=True, size=13)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws['M2'] = vendor or 'All Vendors'
    ws['M2'].font = Font(bold=True, size=10)
    ws['M2'].alignment = Alignment(horizontal='center', vertical='center')
    
    # Logo cell at N (Rows 1-2 merged)
    ws.merge_cells('N1:N2')
    _lub_logo = _tyre_logo_image(width=52, height=52)
    if _lub_logo is not None:
        ws.add_image(_lub_logo, 'N1')
    
    # Row 3: Headers
    headers_map = {
        'A': 'SL NO', 'B': 'DATE', 'C': 'LOCATION', 'D': 'WO NO', 'E': 'REG NO',
        'F': 'PARTICULARS', 'J': 'QTY', 'K': 'RATE', 'L': 'AMOUNT',
        'M': 'COST OF MAN POWER', 'N': 'TOTAL AMOUNT'
    }
    ws.merge_cells('F3:I3')
    for col, val in headers_map.items():
        cell = ws[f'{col}3']
        cell.value = val
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = border
    
    # Column widths
    col_widths = {'A': 7, 'B': 12, 'C': 12, 'D': 24, 'E': 18, 'F': 14, 'G': 5, 'H': 5, 'I': 5, 'J': 8, 'K': 8, 'L': 12, 'M': 18, 'N': 14}
    for col, width in col_widths.items():
        ws.column_dimensions[col].width = width
    ws.row_dimensions[3].height = 22
    
    # Data rows
    row_num = 4
    total_amount_sum = 0
    
    for i, log in enumerate(logs, 1):
        qty_str = f"{log.qty}{log.unit}"
        
        ws.cell(row=row_num, column=1, value=i)
        ws.cell(row=row_num, column=2, value=log.date.strftime('%d.%m.%y') if log.date else '')
        ws.cell(row=row_num, column=3, value=log.location or '')
        ws.cell(row=row_num, column=4, value=log.work_order_no or '')
        ws.cell(row=row_num, column=5, value=log.vehicle.regn)
        ws.cell(row=row_num, column=6, value=log.oil_type)
        ws.cell(row=row_num, column=10, value=qty_str)
        ws.cell(row=row_num, column=11, value=float(log.rate))
        ws.cell(row=row_num, column=12, value=float(log.amount))
        ws.cell(row=row_num, column=13, value=float(log.manpower_cost) if log.manpower_cost else None)
        ws.cell(row=row_num, column=14, value=float(log.total_amount))
        
        total_amount_sum += float(log.total_amount)
        row_num += 1
    
    # Grand Total row
    total_row = row_num
    ws.cell(row=total_row, column=12, value='GRAND TOTAL')
    ws.cell(row=total_row, column=12).font = Font(bold=True)
    ws.cell(row=total_row, column=12).fill = total_fill
    ws.cell(row=total_row, column=14, value=round(total_amount_sum, 2))
    ws.cell(row=total_row, column=14).font = Font(bold=True)
    ws.cell(row=total_row, column=14).fill = total_fill
    
    # Blank row
    ws.cell(row=row_num, column=1, value='')
    row_num += 1
    
    # Signature Section (like in actual debit note)
    sig_row = row_num + 1
    sig_fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
    sig_font = Font(bold=True, size=10)
    sig_align = Alignment(horizontal='center', vertical='center')
    
    sig_headers = ['PREPARED BY', 'P&M MANAGER', 'PROJECT MANAGER', 'PROJECT DIRECTOR', 'ACCEPTED BY SUB-CONTRACTOR']
    sig_cols = [(1,2), (3,5), (6,8), (9,11), (12,14)]  # (start_col, end_col) to merge
    
    sig_border = Border(
        left=Side(border_style='medium', color='000000'),
        right=Side(border_style='medium', color='000000'),
        top=Side(border_style='medium', color='000000'),
        bottom=Side(border_style='medium', color='000000'),
    )
    for (start_col, end_col), label in zip(sig_cols, sig_headers):
        ws.merge_cells(start_row=sig_row, start_column=start_col, end_row=sig_row+3, end_column=end_col)
        cell = ws.cell(row=sig_row, column=start_col, value=label)
        cell.fill = sig_fill
        cell.font = sig_font
        cell.alignment = sig_align
        # Border + fill on EVERY cell of the merged range so the outline
        # wraps the whole block (otherwise lines cut through the middle).
        for rr in range(sig_row, sig_row + 4):
            for cc in range(start_col, end_col + 1):
                ws.cell(row=rr, column=cc).border = sig_border
                ws.cell(row=rr, column=cc).fill = sig_fill
    ws.row_dimensions[sig_row].height = 60
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Lubricants_Consumed_Export.xlsx'
    wb.save(response)
    return response


@login_required
@require_POST
def api_delete_lubrication(request, log_id):
    try:
        from django.utils import timezone
        log = LubricationLog.objects.get(id=log_id)
        log.is_deleted = True
        log.deleted_at = timezone.now()
        log.deleted_by = request.user
        log.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
        from django.contrib import messages
        messages.success(request, 'Entry deleted. You can restore it from Admin Panel.')
    except LubricationLog.DoesNotExist:
        pass
    return redirect('/fleet/lubrication/')


@login_required
@require_POST
def api_edit_lubrication(request, log_id):
    from decimal import Decimal
    import datetime
    from .models import get_or_create_fleet_vehicle
    try:
        log = LubricationLog.objects.get(id=log_id)
        vehicle_input = (request.POST.get('vehicle_regn') or request.POST.get('vehicle_id') or '').strip()
        date_str = request.POST.get('date')
        log.vendor = (request.POST.get('vendor') or '').strip()
        log.location = (request.POST.get('location') or '').strip()
        log.work_order_no = (request.POST.get('work_order_no') or '').strip()
        
        if vehicle_input:
            v_obj = get_or_create_fleet_vehicle(
                vehicle_input,
                vendor=log.vendor,
                work_order_no=log.work_order_no,
                location=log.location
            )
            if v_obj:
                log.vehicle = v_obj
            
        if not log.vendor and log.vehicle_id:
            hv = HiredVehicle.objects.filter(regn__iexact=log.vehicle.regn).first()
            log.vendor = (hv.owner_name if hv else '') or (log.vehicle.extra_data.get('owner_name', '') if isinstance(log.vehicle.extra_data, dict) else '')
        if not log.work_order_no and log.vehicle_id:
            hv = HiredVehicle.objects.filter(regn__iexact=log.vehicle.regn).first()
            log.work_order_no = (hv.agreement_ref if hv else '') or (log.vehicle.extra_data.get('contract_ref', '') if isinstance(log.vehicle.extra_data, dict) else '')
            
        log.oil_type = (request.POST.get('oil_type') or '').strip() or log.oil_type or 'General Lubricant'
        
        try:
            log.qty = float(request.POST.get('qty') or 0.0)
        except (ValueError, TypeError):
            pass
            
        log.unit = (request.POST.get('unit') or log.unit or 'L').strip()
        
        try:
            log.rate = Decimal(str(request.POST.get('rate') or '0.00'))
        except Exception:
            pass
            
        try:
            log.manpower_cost = Decimal(str(request.POST.get('manpower_cost') or '0.00'))
        except Exception:
            pass
            
        log.amount = Decimal(str(log.qty)) * log.rate
        log.total_amount = log.amount + (log.manpower_cost or Decimal('0.00'))
        if date_str:
            try:
                log.date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        log.save()
        from django.contrib import messages
        messages.success(request, 'Entry updated successfully.')
    except Exception as e:
        from django.contrib import messages
        messages.error(request, f'Error: {str(e)}')
    return redirect('/fleet/lubrication/')


@login_required
def api_export_lubrication_pdf(request):
    """Render a beautiful print-ready HTML page for PDF generation"""
    logs = LubricationLog.objects.select_related('vehicle').all().order_by('date', 'id')
    
    # Apply Filters from GET parameters
    start_date = request.GET.get('start_date') or request.GET.get('date_from')
    end_date = request.GET.get('end_date') or request.GET.get('date_to')
    vehicle_id = request.GET.get('vehicle_id')
    location = request.GET.get('location')
    oil_type = request.GET.get('oil_type')
    vendor = (request.GET.get('vendor') or '').strip()
    work_order_q = (request.GET.get('work_order_no') or request.GET.get('wo') or '').strip()
    
    if start_date:
        logs = logs.filter(date__gte=start_date)
    if end_date:
        logs = logs.filter(date__lte=end_date)
    if vehicle_id:
        logs = logs.filter(vehicle_id=vehicle_id)
    if location:
        logs = logs.filter(location__iexact=location)
    if oil_type:
        logs = logs.filter(oil_type__iexact=oil_type)
    if vendor:
        logs = logs.filter(vendor__iexact=vendor)
    if work_order_q:
        logs = logs.filter(work_order_no__icontains=work_order_q)
        
    # Calculate Grand Total
    total_amount_sum = sum(log.total_amount for log in logs)
    
    import datetime
    context = {
        'logs': logs,
        'total_amount_sum': total_amount_sum,
        'today': datetime.date.today(),
        'start_date': start_date,
        'end_date': end_date,
        'vendor_label': vendor or 'All Vendors',
        'logo_data_uri': _tyre_logo_data_uri(),
    }
    return render(request, 'fleet/lubrication_pdf.html', context)


@login_required
def api_lubrication_poll(request):
    """Lightweight JSON endpoint for live-polling: returns latest entry id, count and timestamp."""
    import datetime
    from django.http import JsonResponse
    latest = LubricationLog.objects.order_by('-id').first()
    return JsonResponse({
        'count': LubricationLog.objects.count(),
        'latest_id': latest.id if latest else 0,
        'latest_ts': latest.entered_on.isoformat() if latest else None,
        'server_time': datetime.datetime.now().isoformat(),
    })

@login_required
def api_live_lubrication(request):
    """Return new lubrication entries since ?since_id=X as JSON."""
    since_id = int(request.GET.get('since_id', 0))
    from fleet.models import LubricationLog
    logs = LubricationLog.objects.select_related('vehicle', 'entered_by').filter(id__gt=since_id).order_by('-id')[:50]
    data = []
    for log in logs:
        data.append({
            'id': log.id,
            'date': log.date.strftime('%d-%m-%Y') if log.date else '',
            'vehicle_reg': log.vehicle.regn if log.vehicle else '',
            'location': log.location or '',
            'work_order_no': log.work_order_no or '',
            'vendor': log.vendor or '',
            'oil_type': log.oil_type or '',
            'qty': str(log.qty or 0),
            'unit': log.unit or '',
            'rate': str(log.rate or 0),
            'amount': str(log.amount or 0),
            'manpower_cost': str(log.manpower_cost or 0),
            'total_amount': str(log.total_amount or 0),
            'entered_by': (log.entered_by.full_name or log.entered_by.username) if log.entered_by else 'System',
            'time': localtime(log.entered_on).strftime('%I:%M %p') if log.entered_on else '',
            'entered_on_str': localtime(log.entered_on).strftime('%d %b, %I:%M %p') if log.entered_on else '',
        })
    from fleet.models import LubricationLog as LL
    total = LL.objects.count()
    latest = LL.objects.order_by('-id').first()
    return JsonResponse({'rows': data, 'count': total, 'latest_id': latest.id if latest else 0})


# ========== TYRE SECTION ==========
from .models import TyreLog


def _can_access_tyre(user):
    return user.is_superuser or user.system_role == 'MANAGER' or 'tyre_section' in (user.assigned_modules or [])


def _tyre_logo_image(width=52, height=52):
    """Company logo (RIGSAR - VAJRA) as an openpyxl image for Excel exports."""
    import os
    from django.conf import settings
    from openpyxl.drawing.image import Image as XLImage
    path = os.path.join(settings.BASE_DIR, 'media', 'branding', 'rigsar_vajra_logo.png')
    if os.path.exists(path):
        img = XLImage(path)
        img.width = width
        img.height = height
        return img
    return None


def _tyre_logo_data_uri():
    import base64
    import os
    from django.conf import settings
    path = os.path.join(settings.BASE_DIR, 'media', 'branding', 'rigsar_vajra_logo.png')
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            return 'data:image/png;base64,' + base64.b64encode(fh.read()).decode()
    return ''


@login_required
def tyre_view(request):
    if not _can_access_tyre(request.user):
        messages.error(request, 'Tyre Section is not assigned to your account.')
        return redirect('dashboard')
    from django.db.models import Sum
    import datetime, calendar

    logs = TyreLog.objects.select_related('vehicle', 'entered_by').filter(is_deleted=False).order_by('-date', '-id')
    vehicles = FleetVehicle.objects.filter(is_active=True).order_by('regn')

    today = datetime.date.today()
    month_start = today.replace(day=1)
    month_logs = TyreLog.objects.filter(date__gte=month_start, is_deleted=False)
    stats_month = 'This Month'
    if not month_logs.exists():
        latest_any = TyreLog.objects.filter(is_deleted=False).order_by('-date').first()
        if latest_any:
            month_start = latest_any.date.replace(day=1)
            _, last_day = calendar.monthrange(month_start.year, month_start.month)
            month_logs = TyreLog.objects.filter(date__range=(month_start, month_start.replace(day=last_day)), is_deleted=False)
            stats_month = month_start.strftime('%b %Y')

    month_amount = month_logs.aggregate(t=Sum('total_amount'))['t'] or 0
    month_punctures = month_logs.filter(entry_type='PUNCTURE').aggregate(t=Sum('punctures'))['t'] or 0
    month_issued_count = month_logs.filter(entry_type='ISSUE').count()

    locations = sorted(set(v for v in TyreLog.objects.filter(is_deleted=False).values_list('location', flat=True).distinct() if v))
    work_orders = sorted(set(v for v in TyreLog.objects.filter(is_deleted=False).values_list('work_order_no', flat=True).distinct() if v))
    
    # Vendor / Owner names from TyreLog, HiredVehicle, and FleetVehicle master
    vendor_set = set(v.strip() for v in TyreLog.objects.filter(is_deleted=False).values_list('vendor', flat=True).distinct() if v and v.strip())
    for hv_owner in HiredVehicle.objects.exclude(owner_name='').values_list('owner_name', flat=True):
        if hv_owner and hv_owner.strip():
            vendor_set.add(hv_owner.strip())
    for fv in FleetVehicle.objects.all():
        if isinstance(fv.extra_data, dict):
            oname = (fv.extra_data.get('owner_name') or '').strip()
            if oname:
                vendor_set.add(oname)
    vendors = sorted(vendor_set, key=lambda s: s.lower())

    companies = sorted(set(v for v in TyreLog.objects.filter(is_deleted=False).values_list('company', flat=True).distinct() if v))
    sizes = sorted(set(v for v in TyreLog.objects.filter(is_deleted=False).values_list('size_of_tyre', flat=True).distinct() if v))
    ply_numbers = sorted(set(v for v in TyreLog.objects.filter(is_deleted=False).values_list('ply_number', flat=True).distinct() if v))
    
    # Driver & Operator names from TyreLog, VehicleMovement, Driver master, Employee directory, and Portal Vehicles
    from portal.models import Employee, Vehicle as PortalVehicle
    driver_set = set(v for v in TyreLog.objects.filter(is_deleted=False).values_list('driver_name', flat=True).distinct() if v)
    for dn in VehicleMovement.objects.exclude(driver_name='').values_list('driver_name', flat=True):
        if dn and dn.strip(): driver_set.add(dn.strip())
    for dr in Driver.objects.filter(is_active=True).values_list('name', flat=True):
        if dr and dr.strip(): driver_set.add(dr.strip())
    for emp in Employee.objects.exclude(name='').values_list('name', flat=True):
        if emp and emp.strip(): driver_set.add(emp.strip())
    for pv in PortalVehicle.objects.exclude(driver_name='').values_list('driver_name', flat=True):
        if pv and pv.strip(): driver_set.add(pv.strip())
    drivers = sorted(driver_set, key=lambda s: s.lower())

    hired_vehicles = list(HiredVehicle.objects.exclude(owner_name='').values('regn', 'equipment_type', 'owner_name', 'agreement_ref'))
    latest_log = TyreLog.objects.filter(is_deleted=False).order_by('-id').first()

    context = {
        'logs': logs,
        'vehicles': vehicles,
        'total_entries': logs.count(),
        'month_amount': round(month_amount, 2),
        'month_punctures': month_punctures,
        'month_issued_count': month_issued_count,
        'stats_month': stats_month,
        'locations': locations,
        'work_orders': work_orders,
        'vendors': vendors,
        'companies': companies,
        'sizes': sizes,
        'ply_numbers': ply_numbers,
        'drivers': drivers,
        'hired_vehicles': json.dumps(hired_vehicles),
        'latest_log_id': latest_log.id if latest_log else 0,
        'total_log_count': logs.count(),
    }
    return render(request, 'fleet/tyre.html', context)


@login_required
@require_POST
def api_add_tyre(request):
    import datetime
    from decimal import Decimal
    from .models import get_or_create_fleet_vehicle
    try:
        def D(v):
            if v is None:
                return Decimal('0')
            s = str(v).strip()
            return Decimal(s) if s else Decimal('0')

        entry_type = (request.POST.get('entry_type') or 'PUNCTURE').strip().upper()
        if entry_type not in ('PUNCTURE', 'ISSUE'):
            entry_type = 'PUNCTURE'

        vehicle_input = (request.POST.get('vehicle_regn') or request.POST.get('vehicle_id') or '').strip()
        if not vehicle_input or vehicle_input == '__NEW__':
            vehicle_input = "General Vehicle"

        vendor = (request.POST.get('vendor') or '').strip()
        location = (request.POST.get('location') or '').strip()
        work_order_no = (request.POST.get('work_order_no') or '').strip()
        
        vehicle = get_or_create_fleet_vehicle(
            vehicle_input,
            vendor=vendor,
            work_order_no=work_order_no,
            location=location
        )
            
        if not vendor and vehicle:
            hv = HiredVehicle.objects.filter(regn__iexact=vehicle.regn).first()
            vendor = (hv.owner_name if hv else '') or (vehicle.extra_data.get('owner_name', '') if isinstance(vehicle.extra_data, dict) else '')
        if not work_order_no and vehicle:
            hv = HiredVehicle.objects.filter(regn__iexact=vehicle.regn).first()
            work_order_no = (hv.agreement_ref if hv else '') or (vehicle.extra_data.get('contract_ref', '') if isinstance(vehicle.extra_data, dict) else '')
            
        date_str = (request.POST.get('date') or '').strip()
        if date_str:
            try:
                date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except Exception:
                date = timezone.now().date()
        else:
            date = timezone.now().date()

        if entry_type == 'ISSUE':
            tyre_number = (request.POST.get('tyre_number') or '').strip()
            company = (request.POST.get('company') or '').strip()
            ply_number = (request.POST.get('ply_number') or '').strip()
            size_of_tyre = (request.POST.get('size_of_tyre') or '').strip()
            driver_name = (request.POST.get('driver_name') or '').strip()
            mat_cost = D(request.POST.get('material_cost'))
            total = D(request.POST.get('total_amount'))
            if total == 0 and mat_cost > 0:
                total = mat_cost

            TyreLog.objects.create(
                entry_type='ISSUE',
                date=date,
                location=location,
                work_order_no=work_order_no,
                vendor=vendor,
                vehicle=vehicle,
                tyre_number=tyre_number,
                company=company,
                ply_number=ply_number,
                size_of_tyre=size_of_tyre,
                driver_name=driver_name,
                material_cost=mat_cost,
                total_amount=total,
                entered_by=request.user,
            )
            messages.success(request, f'New tyre ({tyre_number or company}) issued to vehicle {vehicle.regn} successfully.')
        else:
            material = D(request.POST.get('material_cost'))
            big = D(request.POST.get('big_patches_cost'))
            small = D(request.POST.get('small_patches_cost'))
            opening = D(request.POST.get('opening_fitting_cost'))
            total = material + big + small + opening
            TyreLog.objects.create(
                entry_type='PUNCTURE',
                date=date,
                location=location,
                work_order_no=work_order_no,
                vendor=vendor,
                vehicle=vehicle,
                punctures=int(request.POST.get('punctures') or 0),
                big_patches=int(request.POST.get('big_patches') or 0),
                small_patches=int(request.POST.get('small_patches') or 0),
                nozzles=int(request.POST.get('nozzles') or 0),
                valve_pin_number=(request.POST.get('valve_pin_number') or '').strip(),
                material_cost=material,
                big_patches_cost=big,
                small_patches_cost=small,
                opening_fitting_cost=opening,
                total_amount=total,
                entered_by=request.user,
            )
            messages.success(request, f'Tyre puncture entry for {vehicle.regn} added successfully.')
            log_activity(request.user, 'CREATE', 'Tyre Register', f"Added Puncture entry for {vehicle.regn} (Punctures: {request.POST.get('punctures', 0)})", request)

        add_another = request.POST.get('add_another', '1')
        if add_another == '0':
            return redirect(f'/fleet/tyre/?add_another=1&vehicle_id={vehicle.id}')
    except Exception as e:
        messages.error(request, f'Error: {str(e)}')
    return redirect('/fleet/tyre/')



@login_required
@require_POST
def api_edit_tyre(request, log_id):
    import datetime
    from decimal import Decimal
    from .models import get_or_create_fleet_vehicle
    try:
        def D(v):
            if v is None:
                return Decimal('0')
            s = str(v).strip()
            return Decimal(s) if s else Decimal('0')

        log = TyreLog.objects.get(id=log_id)
        vehicle_input = (request.POST.get('vehicle_regn') or request.POST.get('vehicle_id') or '').strip()
        vendor = (request.POST.get('vendor') or '').strip()
        location = (request.POST.get('location') or '').strip()
        work_order_no = (request.POST.get('work_order_no') or '').strip()
        
        v_obj = get_or_create_fleet_vehicle(
            vehicle_input,
            vendor=vendor,
            work_order_no=work_order_no,
            location=location
        )
        if v_obj:
            log.vehicle = v_obj
            
        date_str = (request.POST.get('date') or '').strip()
        if date_str:
            try:
                log.date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except Exception:
                pass
        log.vendor = vendor
        log.location = location
        log.work_order_no = work_order_no

        entry_type = (request.POST.get('entry_type') or log.entry_type or 'PUNCTURE').strip().upper()
        if entry_type in ('PUNCTURE', 'ISSUE'):
            log.entry_type = entry_type
        
        if not log.vendor:
            hv = HiredVehicle.objects.filter(regn__iexact=log.vehicle.regn if log.vehicle_id else '').first()
            log.vendor = (hv.owner_name if hv else '') or (log.vehicle.extra_data.get('owner_name', '') if isinstance(log.vehicle.extra_data, dict) else '')
        if not log.work_order_no:
            hv = HiredVehicle.objects.filter(regn__iexact=log.vehicle.regn if log.vehicle_id else '').first()
            log.work_order_no = (hv.agreement_ref if hv else '') or (log.vehicle.extra_data.get('contract_ref', '') if isinstance(log.vehicle.extra_data, dict) else '')
            
        if log.entry_type == 'ISSUE':
            log.tyre_number = (request.POST.get('tyre_number') or '').strip()
            log.company = (request.POST.get('company') or '').strip()
            log.ply_number = (request.POST.get('ply_number') or '').strip()
            log.size_of_tyre = (request.POST.get('size_of_tyre') or '').strip()
            log.driver_name = (request.POST.get('driver_name') or '').strip()
            log.material_cost = D(request.POST.get('material_cost'))
            log.total_amount = D(request.POST.get('total_amount'))
            if log.total_amount == 0 and log.material_cost > 0:
                log.total_amount = log.material_cost
        else:
            log.punctures = int(request.POST.get('punctures') or 0)
            log.big_patches = int(request.POST.get('big_patches') or 0)
            log.small_patches = int(request.POST.get('small_patches') or 0)
            log.nozzles = int(request.POST.get('nozzles') or 0)
            log.valve_pin_number = (request.POST.get('valve_pin_number') or '').strip()
            log.material_cost = D(request.POST.get('material_cost'))
            log.big_patches_cost = D(request.POST.get('big_patches_cost'))
            log.small_patches_cost = D(request.POST.get('small_patches_cost'))
            log.opening_fitting_cost = D(request.POST.get('opening_fitting_cost'))
            log.total_amount = log.material_cost + log.big_patches_cost + log.small_patches_cost + log.opening_fitting_cost

        log.save()
        messages.success(request, 'Tyre entry updated successfully.')
        log_activity(request.user, 'UPDATE', 'Tyre Register', f"Updated Tyre entry #{log_id} for {log.vehicle.regn if log.vehicle else 'N/A'}", request)
    except Exception as e:
        messages.error(request, f'Error: {str(e)}')
    return redirect('/fleet/tyre/')


@login_required
@require_POST
def api_delete_tyre(request, log_id):
    try:
        from django.utils import timezone
        log = TyreLog.objects.get(id=log_id)
        regn = log.vehicle.regn if log.vehicle else 'N/A'
        log.is_deleted = True
        log.deleted_at = timezone.now()
        log.deleted_by = request.user
        log.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
        messages.success(request, 'Entry deleted. You can restore it from Admin Panel.')
        log_activity(request.user, 'DELETE', 'Tyre Register', f"Deleted Tyre entry #{log_id} for {regn}", request)
    except TyreLog.DoesNotExist:
        pass
    return redirect('/fleet/tyre/')



def _tyre_parse_date(date_val):
    import datetime
    if date_val is None:
        return None
    if isinstance(date_val, datetime.datetime):
        return date_val.date()
    if isinstance(date_val, datetime.date):
        return date_val
    date_str = str(date_val).strip()
    for fmt in ('%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%y', '%d/%m/%Y'):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except Exception:
            continue
    return None


def _tyre_debit_note_layout(ws, vendor_label=''):
    """Common debit-note header (rows 1-4) + column widths for Tyre exports."""
    from openpyxl.styles import Border, Side

    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Row 1: Project title + logo (top-right)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.merge_cells('A1:N1')
    ws['A1'] = 'Proposed International Airport Project in Gelephu'
    ws['A1'].font = Font(bold=True, size=12)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    
    # Logo cell at O (Rows 1-2 merged)
    ws.merge_cells('O1:O2')
    logo = _tyre_logo_image(width=52, height=52)
    if logo is not None:
        ws.add_image(logo, 'O1')

    # Row 2: Debit note title + unit name
    ws.merge_cells('A2:M2')
    ws['A2'] = 'DEBIT NOTE FOR TYRE PUNCTURE'
    ws['A2'].font = Font(bold=True, size=13)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws['N2'] = vendor_label
    ws['N2'].font = Font(bold=True, size=10)
    ws['N2'].alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    # Row 3: WORK DESCRIPTION group label over cost columns
    ws.merge_cells('K3:N3')
    ws['K3'] = 'WORK DESCRIPTION'
    ws['K3'].font = Font(bold=True, size=10)
    ws['K3'].alignment = Alignment(horizontal='center', vertical='center')

    # Row 4: Headers
    headers = ['SR.NO', 'DATE', 'LOCATION', 'WO NO', 'REG NO', 'No of Puncture', 'Big Patches',
               'Small Patches', 'No of Nozzle', 'Valve Pin Number', 'Material cost in spare tyre removing and fitting in place of puncture tyre.',
               'Cost of Big Patches', 'Cost of Small Patches', 'Cost of Opening and Fitting', 'TOTAL AMOUNT']
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    for ci, val in enumerate(headers, 1):
        cell = ws.cell(row=4, column=ci, value=val)
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF', size=9)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
    ws.row_dimensions[3].height = 18
    ws.row_dimensions[4].height = 52

    widths = [7, 11, 13, 22, 16, 9, 9, 9, 9, 15, 18, 12, 12, 13, 14]
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = w
    return border


@login_required
def api_import_tyre(request):
    if request.method != 'POST':
        return redirect('/fleet/tyre/')
    from decimal import Decimal, InvalidOperation

    excel_file = request.FILES.get('excel_file')
    if not excel_file:
        messages.error(request, 'No file uploaded.')
        return redirect('/fleet/tyre/')

    wb = openpyxl.load_workbook(excel_file, data_only=True)
    errors = []
    imported = 0
    from .models import HiredVehicle as HV

    def to_int(v):
        try:
            return int(float(str(v).strip()))
        except Exception:
            return 0

    def to_dec(v):
        try:
            return Decimal(str(v).strip().replace(',', ''))
        except (InvalidOperation, Exception):
            return Decimal('0')

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        header_row = None
        for row_idx in range(1, 12):
            val = ws.cell(row=row_idx, column=1).value
            if val and str(val).strip().upper().replace('.', '').replace(' ', '') in ('SRNO', 'SLNO'):
                header_row = row_idx
                break
        if header_row is None:
            continue

        current_date = current_location = current_wo = current_regn = None
        for row_idx in range(header_row + 1, ws.max_row + 1):
            row = [ws.cell(row=row_idx, column=c).value for c in range(1, 15)]
            sr_no, date_val, location, wo_no, regn = row[0], row[1], row[2], row[3], row[4]
            punctures, big_p, small_p, nozzles = row[5], row[6], row[7], row[8]
            mat_c, big_c, small_c, open_c, total_c = row[9], row[10], row[11], row[12], row[13]

            if sr_no is None and date_val is None and regn is None and total_c is None:
                continue
            # TOTAL row / signature rows
            if regn is None and (mat_c is not None or total_c is not None) and date_val is None:
                continue

            if regn:
                current_regn = str(regn).strip()
            if date_val:
                current_date = _tyre_parse_date(date_val)
            if location:
                current_location = str(location).strip()
            if wo_no:
                current_wo = str(wo_no).strip()

            if not current_regn:
                continue
            try:
                vehicle = FleetVehicle.objects.get(regn__iexact=current_regn)
            except FleetVehicle.DoesNotExist:
                errors.append(f"Sheet '{sheet_name}' Row {row_idx}: Vehicle '{current_regn}' not found.")
                continue
            if current_date is None:
                errors.append(f"Sheet '{sheet_name}' Row {row_idx}: Could not parse date.")
                continue

            material = to_dec(mat_c)
            big = to_dec(big_c)
            small = to_dec(small_c)
            opening = to_dec(open_c)
            computed = material + big + small + opening
            total = computed if computed > 0 else to_dec(total_c)

            TyreLog.objects.create(
                date=current_date,
                location=current_location or '',
                work_order_no=current_wo or '',
                vendor=(sheet_name or '') or ((HV.objects.filter(regn__iexact=current_regn).first().owner_name or '') if HV.objects.filter(regn__iexact=current_regn).exists() else ''),
                vehicle=vehicle,
                punctures=to_int(punctures),
                big_patches=to_int(big_p),
                small_patches=to_int(small_p),
                nozzles=to_int(nozzles),
                material_cost=material,
                big_patches_cost=big,
                small_patches_cost=small,
                opening_fitting_cost=opening,
                total_amount=total,
                entered_by=request.user,
            )
            imported += 1

    if imported:
        messages.success(request, f'{imported} tyre entries imported successfully.' + (f' {len(errors)} errors skipped.' if errors else ''))
    else:
        messages.error(request, f'No entries imported. {len(errors)} errors found.')
    if errors and not imported:
        messages.warning(request, ' | '.join(errors[:5]))
    return redirect('/fleet/tyre/')


@login_required
def download_tyre_sample(request):
    """Sample Excel in the same format as the tyre debit note."""
    from openpyxl.styles import Border, Side
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Sample Tyre'
    border = _tyre_debit_note_layout(ws)

    sample_rows = [
        (1, '13.07.26', 'Gelephu', 'RVJ/HA/HI/26-03-8', 'BP 4 A 0049', 1, 1, 0, 0, 500.00, 500.00, None, None, 1000.00),
        (2, '17.07.26', 'Gelephu', 'RVJ/HA/HI/26-03-8', 'BP 4 A 0049', 0, 0, 0, 0, None, None, None, 300.00, 300.00),
    ]
    for row_data in sample_rows:
        ws.append(row_data)
    for row in ws.iter_rows(min_row=5, max_row=ws.max_row, max_col=14):
        for cell in row:
            cell.border = border

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Tyre_Sample_Format.xlsx'
    wb.save(response)
    return response


def _tyre_filtered_logs(request):
    from django.db.models import Q
    logs = TyreLog.objects.select_related('vehicle').filter(is_deleted=False).order_by('date', 'id')
    start_date = request.GET.get('start_date') or request.GET.get('date_from')
    end_date = request.GET.get('end_date') or request.GET.get('date_to')
    vehicle_id = request.GET.get('vehicle_id')
    location = request.GET.get('location')
    vendor = request.GET.get('vendor')
    work_order_q = request.GET.get('work_order_no') or request.GET.get('wo')
    entry_type_f = request.GET.get('entry_type')
    company_f = request.GET.get('company')
    driver_f = request.GET.get('driver') or request.GET.get('driver_name')
    size_f = request.GET.get('size_of_tyre') or request.GET.get('size')

    if start_date:
        logs = logs.filter(date__gte=start_date)
    if end_date:
        logs = logs.filter(date__lte=end_date)
    if vehicle_id:
        logs = logs.filter(vehicle_id=vehicle_id)
    if location:
        logs = logs.filter(location__icontains=location)
    if vendor:
        v_clean = vendor.strip().lower()
        matching_hired_regns = set(HiredVehicle.objects.filter(owner_name__icontains=v_clean).values_list('regn', flat=True))
        matching_veh_ids = []
        for fv in FleetVehicle.objects.all():
            regn = fv.regn or ''
            if (regn and regn.upper() in [r.upper() for r in matching_hired_regns]) or (isinstance(fv.extra_data, dict) and v_clean in str(fv.extra_data.get('owner_name', '')).lower()):
                matching_veh_ids.append(fv.id)
        
        logs = logs.filter(
            Q(vendor__icontains=v_clean) | Q(vehicle_id__in=matching_veh_ids)
        )
    if work_order_q:
        logs = logs.filter(work_order_no__icontains=work_order_q)
    if entry_type_f:
        logs = logs.filter(entry_type__iexact=entry_type_f)
    if company_f:
        logs = logs.filter(company__icontains=company_f)
    if driver_f:
        logs = logs.filter(driver_name__icontains=driver_f)
    if size_f:
        logs = logs.filter(size_of_tyre__icontains=size_f)
    return logs


def _tyre_debit_note_layout(ws, vendor_label='', entry_type=''):
    """Common debit-note header (rows 1-4) + column widths for Tyre exports."""
    from openpyxl.styles import Border, Side, Font, PatternFill, Alignment

    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    entry_type = (entry_type or '').strip().upper()
    
    if entry_type == 'ISSUE':
        title = 'DEBIT NOTE FOR ISSUED TYRES'
        headers = ['SR.NO', 'DATE', 'LOCATION', 'WO NO', 'REG NO', 'TYRE NUMBER', 'BRAND / COMPANY', 'SIZE OF TYRE', 'PLY NUMBER', 'DRIVER NAME', 'TOTAL AMOUNT']
        widths = [7, 11, 14, 22, 16, 18, 18, 14, 12, 20, 14]
        title_merge = 'A2:I2'
        vendor_col = 'J2'
        logo_col = 'K1'
        total_merge_cols = (1, 10)
        total_val_col = 11
    elif entry_type == 'PUNCTURE':
        title = 'DEBIT NOTE FOR TYRE PUNCTURE'
        headers = ['SR.NO', 'DATE', 'LOCATION', 'WO NO', 'REG NO', 'No of Puncture', 'Big Patches',
                   'Small Patches', 'No of Nozzle', 'Valve Pin Number', 'Material Cost',
                   'Cost of Big Patches', 'Cost of Small Patches', 'Cost of Opening & Fitting', 'TOTAL AMOUNT']
        widths = [7, 11, 13, 22, 16, 9, 9, 9, 9, 15, 18, 12, 12, 13, 14]
        title_merge = 'A2:M2'
        vendor_col = 'N2'
        logo_col = 'O1'
        total_merge_cols = (11, 14)
        total_val_col = 15
    else:
        title = 'DEBIT NOTE FOR TYRE REGISTER (PUNCTURE & ISSUED)'
        headers = ['SR.NO', 'DATE', 'ENTRY TYPE', 'LOCATION', 'WO NO', 'REG NO', 'TYRE NO / PUNCTURES', 'BRAND / PATCHES', 'SIZE & PLY / NOZZLES', 'DRIVER / VALVE PIN', 'TOTAL AMOUNT']
        widths = [7, 11, 14, 14, 22, 16, 20, 20, 18, 20, 14]
        title_merge = 'A2:I2'
        vendor_col = 'J2'
        logo_col = 'K1'
        total_merge_cols = (1, 10)
        total_val_col = 11

    # Row 1: Project title + logo (top-right)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    last_col_letter = openpyxl.utils.get_column_letter(len(headers) - 1)
    ws.merge_cells(f'A1:{last_col_letter}1')
    ws['A1'] = 'Proposed International Airport Project in Gelephu'
    ws['A1'].font = Font(bold=True, size=12)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')

    # Logo cell
    last_col_full = openpyxl.utils.get_column_letter(len(headers))
    ws.merge_cells(f'{last_col_full}1:{last_col_full}2')
    logo = _tyre_logo_image(width=52, height=52)
    if logo is not None:
        ws.add_image(logo, f'{last_col_full}1')

    # Row 2: Debit note title + unit name
    ws.merge_cells(title_merge)
    ws['A2'] = title
    ws['A2'].font = Font(bold=True, size=13)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws[vendor_col] = vendor_label
    ws[vendor_col].font = Font(bold=True, size=10)
    ws[vendor_col].alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    # Row 4: Headers
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    for ci, val in enumerate(headers, 1):
        cell = ws.cell(row=4, column=ci, value=val)
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF', size=9)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
    ws.row_dimensions[3].height = 14
    ws.row_dimensions[4].height = 42

    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = w
    return border, total_merge_cols, total_val_col


@login_required
def api_export_tyre(request):
    """Export in the Debit Note for Tyre Puncture / Issue format (Excel) with logo."""
    from openpyxl.styles import Border, Side, PatternFill, Font, Alignment
    if not _can_access_tyre(request.user):
        return redirect('dashboard')

    logs = _tyre_filtered_logs(request)
    vendor = (request.GET.get('vendor') or '').strip()
    entry_type = (request.GET.get('entry_type') or '').strip().upper()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Tyre Register'
    border, total_merge_cols, total_val_col = _tyre_debit_note_layout(ws, vendor_label=vendor, entry_type=entry_type)
    total_fill = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')

    row_num = 5
    total_amount_sum = 0
    for i, log in enumerate(logs, 1):
        if entry_type == 'ISSUE':
            values = [
                i,
                log.date.strftime('%d.%m.%y') if log.date else '',
                log.location or '',
                log.work_order_no or '',
                log.vehicle.regn if log.vehicle else '',
                log.tyre_number or '',
                log.company or '',
                log.size_of_tyre or '',
                log.ply_number or '',
                log.driver_name or '',
                float(log.total_amount),
            ]
        elif entry_type == 'PUNCTURE':
            values = [
                i,
                log.date.strftime('%d.%m.%y') if log.date else '',
                log.location or '',
                log.work_order_no or '',
                log.vehicle.regn if log.vehicle else '',
                log.punctures,
                log.big_patches,
                log.small_patches,
                log.nozzles,
                log.valve_pin_number or '',
                float(log.material_cost) if log.material_cost else None,
                float(log.big_patches_cost) if log.big_patches_cost else None,
                float(log.small_patches_cost) if log.small_patches_cost else None,
                float(log.opening_fitting_cost) if log.opening_fitting_cost else None,
                float(log.total_amount),
            ]
        else:
            if log.entry_type == 'ISSUE':
                values = [
                    i,
                    log.date.strftime('%d.%m.%y') if log.date else '',
                    'Issued',
                    log.location or '',
                    log.work_order_no or '',
                    log.vehicle.regn if log.vehicle else '',
                    f"Tyre No: {log.tyre_number or '-'}",
                    f"Brand: {log.company or '-'}",
                    f"Size: {log.size_of_tyre or '-'} | Ply: {log.ply_number or '-'}",
                    f"Driver: {log.driver_name or '-'}",
                    float(log.total_amount),
                ]
            else:
                values = [
                    i,
                    log.date.strftime('%d.%m.%y') if log.date else '',
                    'Puncture',
                    log.location or '',
                    log.work_order_no or '',
                    log.vehicle.regn if log.vehicle else '',
                    f"Punctures: {log.punctures}",
                    f"Big: {log.big_patches} | Small: {log.small_patches}",
                    f"Nozzles: {log.nozzles}",
                    f"Pin: {log.valve_pin_number or '-'}",
                    float(log.total_amount),
                ]
        for ci, val in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=ci, value=val)
            cell.border = border
            if ci == len(values) and isinstance(val, (int, float)):
                cell.number_format = '#,##0.00'
        total_amount_sum += float(log.total_amount)
        row_num += 1

    # TOTAL row
    start_c, end_c = total_merge_cols
    ws.merge_cells(start_row=row_num, start_column=start_c, end_row=row_num, end_column=end_c)
    lbl = ws.cell(row=row_num, column=start_c, value='TOTAL AMOUNT')
    lbl.font = Font(bold=True)
    lbl.alignment = Alignment(horizontal='right', vertical='center')
    tot = ws.cell(row=row_num, column=total_val_col, value=round(total_amount_sum, 2))
    tot.font = Font(bold=True)
    tot.fill = total_fill
    tot.number_format = '#,##0.00'
    for ci in range(1, total_val_col + 1):
        ws.cell(row=row_num, column=ci).border = border
        ws.cell(row=row_num, column=ci).fill = total_fill
    row_num += 2

    # Signature section
    sig_row = row_num + 1
    sig_fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
    sig_headers = ['PREPARED BY', 'P&M MANAGER', 'PROJECT MANAGER', 'PROJECT DIRECTOR', 'ACCEPTED BY SUB-CONTRACTOR']
    num_cols = total_val_col
    col_chunk = max(1, num_cols // 5)
    sig_border = Border(
        left=Side(border_style='medium', color='000000'),
        right=Side(border_style='medium', color='000000'),
        top=Side(border_style='medium', color='000000'),
        bottom=Side(border_style='medium', color='000000'),
    )
    for idx, label in enumerate(sig_headers):
        start_col = idx * col_chunk + 1
        end_col = (idx + 1) * col_chunk if idx < 4 else num_cols
        ws.merge_cells(start_row=sig_row, start_column=start_col, end_row=sig_row + 3, end_column=end_col)
        cell = ws.cell(row=sig_row, column=start_col, value=label)
        cell.fill = sig_fill
        cell.font = Font(bold=True, size=10)
        cell.alignment = Alignment(horizontal='center', vertical='center')
        for rr in range(sig_row, sig_row + 4):
            for cc in range(start_col, end_col + 1):
                ws.cell(row=rr, column=cc).border = sig_border
                ws.cell(row=rr, column=cc).fill = sig_fill
    ws.row_dimensions[sig_row].height = 60

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Tyre_Register_Export.xlsx'
    wb.save(response)
    return response


@login_required
def api_export_tyre_pdf(request):
    """Print-ready HTML page for PDF generation (with logo)."""
    if not _can_access_tyre(request.user):
        return redirect('dashboard')
    logs = _tyre_filtered_logs(request)
    total_amount_sum = sum(log.total_amount for log in logs)
    entry_type = (request.GET.get('entry_type') or '').strip().upper()
    import datetime
    context = {
        'logs': logs,
        'total_amount_sum': total_amount_sum,
        'today': datetime.date.today(),
        'start_date': request.GET.get('start_date'),
        'end_date': request.GET.get('end_date'),
        'vendor_label': request.GET.get('vendor') or '',
        'entry_type': entry_type,
        'logo_data_uri': _tyre_logo_data_uri(),
    }
    return render(request, 'fleet/tyre_pdf.html', context)


@login_required
def api_tyre_poll(request):
    import datetime
    latest = TyreLog.objects.order_by('-id').first()
    return JsonResponse({
        'count': TyreLog.objects.count(),
        'latest_id': latest.id if latest else 0,
        'latest_ts': latest.entered_on.isoformat() if latest else None,
        'server_time': datetime.datetime.now().isoformat(),
    })


@login_required
def api_live_tyre(request):
    from django.utils.timezone import localtime
    since_id = int(request.GET.get('since_id', 0))
    logs = TyreLog.objects.select_related('vehicle', 'entered_by').filter(id__gt=since_id).order_by('-id')[:50]
    data = []
    for log in logs:
        data.append({
            'id': log.id,
            'entry_type': log.entry_type,
            'date': log.date.strftime('%d-%m-%Y') if log.date else '',
            'vehicle_reg': log.vehicle.regn if log.vehicle else '',
            'location': log.location or '',
            'work_order_no': log.work_order_no or '',
            'vendor': log.vendor or '',
            'punctures': log.punctures,
            'big_patches': log.big_patches,
            'small_patches': log.small_patches,
            'nozzles': log.nozzles,
            'valve_pin_number': log.valve_pin_number or '',
            'material_cost': str(log.material_cost or 0),
            'big_patches_cost': str(log.big_patches_cost or 0),
            'small_patches_cost': str(log.small_patches_cost or 0),
            'opening_fitting_cost': str(log.opening_fitting_cost or 0),
            'total_amount': str(log.total_amount or 0),
            'tyre_number': log.tyre_number or '',
            'company': log.company or '',
            'ply_number': log.ply_number or '',
            'size_of_tyre': log.size_of_tyre or '',
            'driver_name': log.driver_name or '',
            'entered_by': (log.entered_by.full_name or log.entered_by.username) if log.entered_by else 'System',
            'time': localtime(log.entered_on).strftime('%I:%M %p') if log.entered_on else '',
            'entered_on_str': localtime(log.entered_on).strftime('%d.%m.%y, %I:%M %p') if log.entered_on else '',
        })
    total = TyreLog.objects.count()
    latest = TyreLog.objects.order_by('-id').first()
    return JsonResponse({'rows': data, 'count': total, 'latest_id': latest.id if latest else 0})


# ========== COMPLETE VEHICLE REPORT (all sections combined) ==========
def _can_access_vehicle_report(user):
    return (user.is_superuser or user.system_role == 'MANAGER'
            or 'lubricants' in (user.assigned_modules or [])
            or 'tyre_section' in (user.assigned_modules or []))


def _vehicle_report_data(request):
    from .models import LubricationLog, SparePartTransaction, TyreLog, VehicleMovement, RepairLog
    from portal.models import DailyDeployment
    from django.db.models import Q
    vehicle = None
    vehicle_id = request.GET.get('vehicle_id')
    regn_q = (request.GET.get('regn') or '').strip()
    if vehicle_id:
        vehicle = FleetVehicle.objects.filter(id=vehicle_id).first()
    elif regn_q:
        vehicle = FleetVehicle.objects.filter(regn__iexact=regn_q).first() or \
                  FleetVehicle.objects.filter(dno__iexact=regn_q).first()
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')

    lub = LubricationLog.objects.select_related('vehicle').filter(vehicle=vehicle).order_by('date', 'id') if vehicle else LubricationLog.objects.none()
    tyre = TyreLog.objects.select_related('vehicle').filter(vehicle=vehicle).order_by('date', 'id') if vehicle else TyreLog.objects.none()
    repairs = vehicle.repair_logs.all().order_by('-in_date') if vehicle else RepairLog.objects.none()
    spares = SparePartTransaction.objects.filter(vehicle=vehicle).select_related('part').order_by('-date') if vehicle else SparePartTransaction.objects.none()
    movements = VehicleMovement.objects.filter(vehicle=vehicle).order_by('-movement_date') if vehicle else VehicleMovement.objects.none()
    
    if vehicle:
        dno_clean = vehicle.dno.strip().lower() if vehicle.dno else ""
        regn_clean = vehicle.regn.strip().lower() if vehicle.regn else ""
        deployments = DailyDeployment.objects.filter(
            Q(machinery__iexact=dno_clean) | Q(machinery__iexact=regn_clean)
        ).order_by('-date')
    else:
        deployments = DailyDeployment.objects.none()

    if start_date:
        lub = lub.filter(date__gte=start_date)
        tyre = tyre.filter(date__gte=start_date)
        repairs = repairs.filter(in_date__gte=start_date)
        spares = spares.filter(date__gte=start_date)
        movements = movements.filter(movement_date__gte=start_date)
        deployments = deployments.filter(date__gte=start_date)
    if end_date:
        lub = lub.filter(date__lte=end_date)
        tyre = tyre.filter(date__lte=end_date)
        repairs = repairs.filter(in_date__lte=end_date)
        spares = spares.filter(date__lte=end_date)
        movements = movements.filter(movement_date__lte=end_date)
        deployments = deployments.filter(date__lte=end_date)

    hired = HiredVehicle.objects.filter(regn__iexact=vehicle.regn).first() if vehicle else None
    return vehicle, hired, lub, tyre, repairs, spares, movements, deployments, start_date, end_date


@login_required
def api_vehicle_report(request):
    """Complete vehicle report: details + repairs + lubricants + spares + tyres + movements in one Excel."""
    from openpyxl.styles import Border, Side
    if not _can_access_vehicle_report(request.user):
        return redirect('dashboard')
        
    # Check parameters (if none are selected, include everything by default)
    inc_repairs = request.GET.get('inc_repairs') == 'true'
    inc_lubricants = request.GET.get('inc_lubricants') == 'true'
    inc_spares = request.GET.get('inc_spares') == 'true'
    inc_tyres = request.GET.get('inc_tyres') == 'true'
    inc_movements = request.GET.get('inc_movements') == 'true'
    
    if not (inc_repairs or inc_lubricants or inc_spares or inc_tyres or inc_movements):
        inc_repairs = inc_lubricants = inc_spares = inc_tyres = inc_movements = True
        
    vehicle, hired, lub, tyre, repairs, spares, movements, deployments, start_date, end_date = _vehicle_report_data(request)
    if vehicle is None:
        messages.error(request, 'Please select a valid vehicle for the complete report.')
        return redirect('fleet:global_dashboard')

    wb = openpyxl.Workbook()
    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    total_fill = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')

    def sheet_header(ws, title, last_col_letter):
        ws.row_dimensions[1].height = 28
        ws.row_dimensions[2].height = 28
        col_idx = openpyxl.utils.column_index_from_string(last_col_letter)
        if col_idx > 1:
            prev_letter = openpyxl.utils.get_column_letter(col_idx - 1)
            ws.merge_cells(f'A1:{prev_letter}1')
            ws.merge_cells(f'A2:{prev_letter}2')
            ws.merge_cells(f'{last_col_letter}1:{last_col_letter}2')
        ws['A1'] = "Proposed International Airport Project in Gelephu"
        ws['A1'].font = Font(bold=True, size=12)
        ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
        ws['A2'] = title
        ws['A2'].font = Font(bold=True, size=13)
        ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
        logo = _tyre_logo_image(width=52, height=52)
        if logo is not None:
            ws.add_image(logo, f'{last_col_letter}1')

    # --- Sheet 1: Vehicle Details + summary ---
    ws = wb.active
    ws.title = 'Vehicle Details'
    sheet_header(ws, 'VEHICLE COMPLETE REPORT', 'D')
    info = [
        ('Registration / Equipment No', vehicle.regn),
        ('Equipment Type', hired.equipment_type if hired else (vehicle.model_name or '')),
        ('Owner / Vendor', hired.owner_name if hired else ''),
        ('Hire By', hired.hire_by if hired else ''),
        ('Hire Agreement Reference', hired.agreement_ref if hired else ''),
        ('Contract Status', hired.contract_status if hired else ''),
        ('Deployment Status', hired.status if hired else ''),
        ('Report Period', f"{start_date or 'All Time'} to {end_date or 'Present'}"),
    ]
    row = 4
    for label, value in info:
        c1 = ws.cell(row=row, column=1, value=label)
        c1.font = Font(bold=True)
        c1.border = border
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        c2 = ws.cell(row=row, column=2, value=value or '-')
        c2.border = border
        for cc in range(2, 5):
            ws.cell(row=row, column=cc).border = border
        row += 1
    row += 1
    
    summary = []
    if inc_repairs:
        summary.append(('Repair & Breakdown Entries', repairs.count(), '-'))
    if inc_lubricants:
        summary.append(('Lubricant Entries', lub.count(), f"Total: {sum(l.total_amount for l in lub)}"))
    if inc_spares:
        summary.append(('Spare Parts Consumed', spares.count(), f"Total: {sum(s.total_amount for s in spares)}"))
    if inc_tyres:
        summary.append(('Tyre Work Entries', tyre.count(), f"Total: {sum(t.total_amount for t in tyre)}"))
    if inc_movements:
        summary.append(('Movements & Allocations', movements.count() + deployments.count(), '-'))

    ws.cell(row=row, column=1, value='SECTION SUMMARY').font = Font(bold=True, size=11)
    row += 1
    for label, count, amount in summary:
        ws.cell(row=row, column=1, value=label).border = border
        ws.cell(row=row, column=2, value=count).border = border
        ws.cell(row=row, column=3, value=amount).border = border
        row += 1
    ws.column_dimensions['A'].width = 30
    ws.column_dimensions['B'].width = 34
    ws.column_dimensions['C'].width = 22
    ws.column_dimensions['D'].width = 16

    # --- Sheet: Repairs & Breakdowns ---
    if inc_repairs and repairs.exists():
        ws_rep = wb.create_sheet('Repairs & Breakdowns')
        sheet_header(ws_rep, 'REPAIRS & BREAKDOWNS LOG', 'H')
        rep_headers = ['SL NO', 'IN DATE', 'OUT DATE', 'MECHANIC', 'BREAKDOWN DETAILS', 'REPAIR DETAILS', 'MANPOWER COST', 'STATUS']
        for ci, h in enumerate(rep_headers, 1):
            cell = ws_rep.cell(row=4, column=ci, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color='FFFFFF', size=9)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        ws_rep.row_dimensions[4].height = 30
        r_rep = 5
        for i, log in enumerate(repairs, 1):
            vals = [
                i, 
                log.in_date.strftime('%d.%m.%y') if log.in_date else '', 
                log.out_date.strftime('%d.%m.%y') if log.out_date else 'In Workshop', 
                getattr(log, 'mechanic', '') or '', 
                getattr(log, 'complaint', '') or '', 
                getattr(log, 'parts_used', '') or '', 
                float(getattr(log, 'manpower_cost', 0) or 0), 
                'Completed' if log.out_date else 'Active'
            ]
            for ci, v in enumerate(vals, 1):
                ws_rep.cell(row=r_rep, column=ci, value=v).border = border
            r_rep += 1
        for col_letter, w in zip('ABCDEFGH', [7, 12, 12, 20, 25, 25, 14, 12]):
            ws_rep.column_dimensions[col_letter].width = w

    # --- Sheet: Lubricants ---
    if inc_lubricants and lub.exists():
        ws2 = wb.create_sheet('Lubricants')
        sheet_header(ws2, 'LUBRICANTS CONSUMED', 'K')
        lub_headers = ['SL NO', 'DATE', 'LOCATION', 'WO NO', 'VENDOR', 'PARTICULARS', 'QTY', 'RATE', 'AMOUNT', 'MAN POWER', 'TOTAL AMOUNT']
        for ci, h in enumerate(lub_headers, 1):
            cell = ws2.cell(row=4, column=ci, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color='FFFFFF', size=9)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        ws2.row_dimensions[4].height = 30
        r2 = 5
        for i, log in enumerate(lub, 1):
            vals = [i, log.date.strftime('%d.%m.%y') if log.date else '', log.location or '',
                    log.work_order_no or '', log.vendor or '', log.oil_type, f"{log.qty}{log.unit}",
                    float(log.rate), float(log.amount), float(log.manpower_cost or 0), float(log.total_amount)]
            for ci, v in enumerate(vals, 1):
                ws2.cell(row=r2, column=ci, value=v).border = border
            r2 += 1
        ws2.cell(row=r2, column=9, value='GRAND TOTAL').font = Font(bold=True)
        gt = ws2.cell(row=r2, column=11, value=float(sum(l.total_amount for l in lub)))
        gt.font = Font(bold=True)
        gt.fill = total_fill
        for w, col in zip([7, 11, 13, 20, 22, 20, 8, 9, 11, 12, 13], 'ABCDEFGHIJK'):
            ws2.column_dimensions[col].width = w

    # --- Sheet: Spare Parts ---
    if inc_spares and spares.exists():
        ws_sp = wb.create_sheet('Spare Parts')
        sheet_header(ws_sp, 'SPARE PARTS CONSUMED', 'J')
        sp_headers = ['SL NO', 'DATE', 'TRANSACTION TYPE', 'PART NAME', 'PART NUMBER', 'QTY', 'RATE', 'MANPOWER COST', 'TOTAL AMOUNT', 'APPROVED']
        for ci, h in enumerate(sp_headers, 1):
            cell = ws_sp.cell(row=4, column=ci, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color='FFFFFF', size=9)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        ws_sp.row_dimensions[4].height = 30
        r_sp = 5
        for i, log in enumerate(spares, 1):
            vals = [
                i, 
                log.date.strftime('%d.%m.%y') if log.date else '', 
                log.transaction_type, 
                log.part.part_name if log.part else (log.custom_part_name or ''), 
                log.part.part_number if log.part else '', 
                log.quantity, 
                float(log.rate or 0), 
                float(log.manpower_cost or 0), 
                float(log.total_amount or 0), 
                'Yes' if log.is_approved else 'Pending'
            ]
            for ci, v in enumerate(vals, 1):
                ws_sp.cell(row=r_sp, column=ci, value=v).border = border
            r_sp += 1
        for col_letter, w in zip('ABCDEFGHIJ', [7, 12, 15, 25, 18, 8, 10, 14, 14, 12]):
            ws_sp.column_dimensions[col_letter].width = w

    # --- Sheet: Tyre Work ---
    if inc_tyres and tyre.exists():
        ws3 = wb.create_sheet('Tyre Work')
        sheet_header(ws3, 'TYRE PUNCTURE WORK', 'N')
        tyre_headers = ['SR.NO', 'DATE', 'LOCATION', 'WO NO', 'VENDOR', 'REG NO', 'Puncture', 'Big P', 'Small P', 'Nozzle',
                        'Material Cost', 'Big Cost', 'Small Cost', 'Opening & Fitting', 'TOTAL AMOUNT']
        for ci, h in enumerate(tyre_headers, 1):
            cell = ws3.cell(row=4, column=ci, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color='FFFFFF', size=9)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        ws3.row_dimensions[4].height = 30
        r3 = 5
        for i, log in enumerate(tyre, 1):
            vals = [i, log.date.strftime('%d.%m.%y') if log.date else '', log.location or '',
                    log.work_order_no or '', log.vendor or '', log.vehicle.regn if log.vehicle else '',
                    log.punctures, log.big_patches, log.small_patches, log.nozzles,
                    float(log.material_cost or 0), float(log.big_patches_cost or 0),
                    float(log.small_patches_cost or 0), float(log.opening_fitting_cost or 0), float(log.total_amount)]
            for ci, v in enumerate(vals, 1):
                ws3.cell(row=r3, column=ci, value=v).border = border
            r3 += 1
        ws3.cell(row=r3, column=14, value='GRAND TOTAL').font = Font(bold=True)
        gt3 = ws3.cell(row=r3, column=15, value=float(sum(t.total_amount for t in tyre)))
        gt3.font = Font(bold=True)
        gt3.fill = total_fill
        for w, col in zip([7, 11, 13, 20, 24, 15, 9, 8, 9, 8, 12, 10, 10, 13, 13], 'ABCDEFGHIJKLMNO'):
            ws3.column_dimensions[col].width = w

    # --- Sheet: Movements & Deployments ---
    if inc_movements and (movements.exists() or deployments.exists()):
        ws_mv = wb.create_sheet('Movements & Deployments')
        
        # Section A: Movements
        sheet_header(ws_mv, 'VEHICLE MOVEMENT LOGS', 'H')
        mv_headers = ['SL NO', 'DATE', 'SHIFT', 'DRIVER', 'FROM SITE', 'TO SITE', 'PURPOSE', 'SUPERVISOR']
        for ci, h in enumerate(mv_headers, 1):
            cell = ws_mv.cell(row=4, column=ci, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color='FFFFFF', size=9)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        ws_mv.row_dimensions[4].height = 30
        r_mv = 5
        for i, log in enumerate(movements, 1):
            vals = [
                i, 
                log.movement_date.strftime('%d.%m.%y') if log.movement_date else '', 
                log.shift or '', 
                log.driver.name if log.driver else '', 
                log.from_location or '', 
                log.to_location or '', 
                log.remarks or '', 
                log.entered_by.full_name if log.entered_by else ''
            ]
            for ci, v in enumerate(vals, 1):
                ws_mv.cell(row=r_mv, column=ci, value=v).border = border
            r_mv += 1
            
        # Section B: Deployments (below movements)
        r_mv += 2
        ws_mv.cell(row=r_mv, column=1, value='SITE ALLOCATION & DEPLOYMENTS').font = Font(bold=True, size=11)
        r_mv += 1
        dep_headers = ['SL NO', 'DATE', 'ZONE 1&2 D/N', 'ZONE 3&4 D/N', 'BORROW AREA D/N', 'CULVERT D/N', 'BATCHING D/N', 'CRUSHING D/N']
        for ci, h in enumerate(dep_headers, 1):
            cell = ws_mv.cell(row=r_mv, column=ci, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color='FFFFFF', size=9)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        ws_mv.row_dimensions[r_mv].height = 25
        r_mv += 1
        for i, log in enumerate(deployments, 1):
            vals = [
                i, 
                log.date.strftime('%d.%m.%y') if log.date else '', 
                f"{log.zone_1_2_day}/{log.zone_1_2_night}", 
                f"{log.zone_3_4_day}/{log.zone_3_4_night}", 
                f"{log.borrow_area_day}/{log.borrow_area_night}", 
                f"{log.culvert_area_day}/{log.culvert_area_night}", 
                f"{log.batching_plant_day}/{log.batching_plant_night}", 
                f"{log.crushing_plant_day}/{log.crushing_plant_night}"
            ]
            for ci, v in enumerate(vals, 1):
                ws_mv.cell(row=r_mv, column=ci, value=v).border = border
            r_mv += 1
        for col_letter, w in zip('ABCDEFGH', [7, 12, 15, 18, 18, 18, 18, 18]):
            ws_mv.column_dimensions[col_letter].width = w

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Vehicle_Full_Report_{vehicle.regn.replace(" ", "_")}.xlsx'
    wb.save(response)
    return response


@login_required
def api_vehicle_report_pdf(request):
    """Print-ready complete vehicle report with selected sections."""
    if not _can_access_vehicle_report(request.user):
        return redirect('dashboard')
        
    inc_repairs = request.GET.get('inc_repairs') == 'true'
    inc_lubricants = request.GET.get('inc_lubricants') == 'true'
    inc_spares = request.GET.get('inc_spares') == 'true'
    inc_tyres = request.GET.get('inc_tyres') == 'true'
    inc_movements = request.GET.get('inc_movements') == 'true'
    
    if not (inc_repairs or inc_lubricants or inc_spares or inc_tyres or inc_movements):
        inc_repairs = inc_lubricants = inc_spares = inc_tyres = inc_movements = True
        
    vehicle, hired, lub, tyre, repairs, spares, movements, deployments, start_date, end_date = _vehicle_report_data(request)
    if vehicle is None:
        messages.error(request, 'Please select a valid vehicle for the complete report.')
        return redirect('fleet:global_dashboard')
    import datetime
    context = {
        'vehicle': vehicle,
        'hired': hired,
        'lub': lub,
        'tyre': tyre,
        'repairs': repairs,
        'spares': spares,
        'movements': movements,
        'deployments': deployments,
        'inc_repairs': inc_repairs,
        'inc_lubricants': inc_lubricants,
        'inc_spares': inc_spares,
        'inc_tyres': inc_tyres,
        'inc_movements': inc_movements,
        'lub_total': sum(l.total_amount for l in lub),
        'tyre_total': sum(t.total_amount for t in tyre),
        'today': datetime.date.today(),
        'start_date': start_date,
        'end_date': end_date,
        'logo_data_uri': _tyre_logo_data_uri(),
    }
    return render(request, 'fleet/vehicle_report_pdf.html', context)


# ==========================================
# SPARE PARTS MODULE
# ==========================================
def get_excel_owner_names():
    from .models import HiredVehicle
    owners = set(HiredVehicle.objects.exclude(owner_name__isnull=True).exclude(owner_name='').values_list('owner_name', flat=True).distinct())
    return sorted(list(owners))


@login_required
def spare_parts_view(request):
    """Main view for the Spare Parts module."""
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser and 'spare_parts' not in (request.user.assigned_modules or []):
        messages.error(request, 'You do not have permission to access Spare Parts management.')
        return redirect('dashboard')
    
    # Base Querysets
    from .models import SparePart, SparePartTransaction
    transactions = SparePartTransaction.objects.select_related('part', 'vehicle', 'entered_by').filter(is_deleted=False)
    parts = SparePart.objects.all()
    vehicles = FleetVehicle.objects.filter(is_active=True).order_by('regn')
    
    # Fetch unique suppliers from DB and combine with Excel owners list
    db_suppliers = list(SparePartTransaction.objects.exclude(supplier__isnull=True).exclude(supplier='').values_list('supplier', flat=True).distinct())
    excel_owners = get_excel_owner_names()
    combined_suppliers = set(db_suppliers)
    for owner in excel_owners:
        combined_suppliers.add(owner)
    suppliers = sorted(list(combined_suppliers))
    
    # Generate vehicle mappings for autofill
    import json
    vehicle_mappings = {}
    import re
    hired_vehicles_map = {re.sub(r'[^A-Z0-9]', '', hv.regn.upper()): hv for hv in HiredVehicle.objects.all() if hv.regn}
    
    for v in vehicles:
        reg_clean = re.sub(r'[^A-Z0-9]', '', v.regn.upper()) if v.regn else ''
        hv = hired_vehicles_map.get(reg_clean)
        owner = hv.owner_name if hv else ''
        contract_ref = hv.agreement_ref if hv else (v.extra_data.get('contract_ref', '') if isinstance(v.extra_data, dict) else '')
        
        vehicle_mappings[v.id] = {
            'location': 'Gelephu',
            'wo_no': contract_ref or '',
            'vendor_name': owner or ''
        }
    vehicle_mappings_json = json.dumps(vehicle_mappings)
    
    # Advanced Filters
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    transaction_type = request.GET.get('transaction_type')
    vehicle_id = request.GET.get('vehicle_id')
    part_id = request.GET.get('part_id')
    vendor_name = request.GET.get('vendor_name') or request.GET.get('supplier')
    
    if start_date:
        transactions = transactions.filter(date__gte=start_date)
    if end_date:
        transactions = transactions.filter(date__lte=end_date)
    if transaction_type:
        transactions = transactions.filter(transaction_type=transaction_type)
    if vehicle_id:
        transactions = transactions.filter(vehicle_id=vehicle_id)
    if part_id:
        transactions = transactions.filter(part_id=part_id)
    if vendor_name:
        transactions = transactions.filter(supplier=vendor_name)
        
    # Summary Calculations
    total_transactions = transactions.count()
    from django.db.models import Sum, F
    total_in = transactions.filter(transaction_type='IN').aggregate(total=Sum('quantity'))['total'] or 0
    total_out = transactions.filter(transaction_type='OUT').aggregate(total=Sum('quantity'))['total'] or 0
    total_stock_value = SparePartTransaction.objects.aggregate(t=Sum('total_amount'))['t'] or 0
    low_stock_count = parts.filter(current_stock__lte=F('reorder_level')).count()

    # Grouped Transactions for UI display
    grouped_tx_raw = _group_spare_transactions(transactions)
    grouped_transactions = []
    for g in grouped_tx_raw:
        first_t = g[0]
        total_amount = sum(t.total_amount for t in g)
        spare_cost = sum(t.quantity * t.rate for t in g)
        manpower_cost = sum(t.manpower_cost for t in g)
        
        items_detail = []
        for idx, t in enumerate(g, 1):
            pname = t.part.part_name if t.part else ""
            pnum = t.part.part_number if t.part else ""
            unit = "Nos" if (t.part and ("No" in t.part.unit or "no" in t.part.unit)) else (t.part.unit if t.part else "")
            items_detail.append({
                'id': t.id,
                'index': idx,
                'part_name': pname,
                'part_number': pnum,
                'quantity': t.quantity,
                'unit': unit,
                'rate': t.rate,
                'rate_formatted': f"{t.rate:,.2f}",
                'total': t.quantity * t.rate,
                'total_formatted': f"{t.quantity * t.rate:,.2f}",
                'remarks': t.remarks or '',
                'obj': t,
            })

        grouped_transactions.append({
            'first_id': first_t.id,
            'all_ids': ",".join([str(t.id) for t in g]),
            'all_ids_list': [t.id for t in g],
            'date': first_t.date,
            'created_at': first_t.created_at,
            'transaction_type': first_t.transaction_type,
            'vehicle': first_t.vehicle,
            'supplier': first_t.supplier,
            'location': first_t.location or 'Gelephu',
            'wo_no': first_t.wo_no or '-',
            'entered_by': first_t.entered_by,
            'is_approved': all(t.is_approved for t in g),
            'approved_by': first_t.approved_by if all(t.is_approved for t in g) else None,
            'items': items_detail,
            'items_count': len(g),
            'spare_cost': spare_cost,
            'spare_cost_formatted': f"{spare_cost:,.2f}",
            'manpower_cost': manpower_cost,
            'manpower_cost_formatted': f"{manpower_cost:,.2f}",
            'total_amount': total_amount,
            'total_amount_formatted': f"{total_amount:,.2f}",
            'primary_part_name': first_t.part.part_name if first_t.part else "",
            'primary_part_number': first_t.part.part_number if first_t.part else "",
        })

    context = {
        'transactions': transactions,
        'grouped_transactions': grouped_transactions,
        'parts': parts,
        'vehicles': vehicles,
        'suppliers': suppliers,
        'vehicle_mappings_json': vehicle_mappings_json,
        'total_transactions': total_transactions,
        'total_in': total_in,
        'total_out': total_out,
        'total_stock_value': total_stock_value,
        'low_stock_count': low_stock_count,
        'is_manager': request.user.system_role == 'MANAGER' or request.user.is_superuser,
        'filters': {
            'start_date': start_date,
            'end_date': end_date,
            'transaction_type': transaction_type,
            'vehicle_id': vehicle_id,
            'part_id': part_id,
            'vendor_name': request.GET.get('vendor_name', ''),
        }
    }
    return render(request, 'fleet/spare_parts.html', context)


@login_required
def api_spare_parts_suppliers(request):
    """API view to fetch all unique suppliers/vendors (excel + database)"""
    from django.http import JsonResponse
    from .models import SparePartTransaction
    db_suppliers = list(SparePartTransaction.objects.exclude(supplier__isnull=True).exclude(supplier='').values_list('supplier', flat=True).distinct())
    excel_owners = get_excel_owner_names()
    combined_suppliers = set(db_suppliers)
    for owner in excel_owners:
        combined_suppliers.add(owner)
    suppliers = sorted(list(combined_suppliers))
    return JsonResponse({'success': True, 'suppliers': suppliers})


@login_required
def api_add_spare_part_transaction(request):
    """Add a new IN or OUT transaction and update stock."""
    if request.method == 'POST':
        try:
            from .models import SparePart, SparePartTransaction
            transaction_type = request.POST.get('transaction_type')
            part_id = request.POST.get('part_id')
            part_number = request.POST.get('part_number')
            part_name = request.POST.get('part_name')
            category = request.POST.get('category')
            date = request.POST.get('date')
            quantity = int(request.POST.get('quantity', 0))
            rate = float(request.POST.get('rate', 0.0))
            from django.db import transaction
            from .models import SparePart, SparePartTransaction, get_or_create_fleet_vehicle
            from django.utils import timezone
            import datetime

            transaction_type = (request.POST.get('transaction_type') or 'OUT').strip().upper()
            date_str = request.POST.get('date')
            date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.now().date()
            
            location = (request.POST.get('location') or '').strip()
            wo_no = (request.POST.get('wo_no') or '').strip()
            supplier_val = (request.POST.get('supplier') or '').strip()
            vehicle_input = (request.POST.get('vehicle_regn') or request.POST.get('vehicle_id') or '').strip()
            manpower_cost = float(request.POST.get('manpower_cost') or 0.0)
            reference_no = request.POST.get('reference_no', '')
            mechanic = request.POST.get('mechanic')
            
            vehicle = None
            supplier = supplier_val or None
            if transaction_type == 'OUT':
                if vehicle_input:
                    vehicle = get_or_create_fleet_vehicle(
                        vehicle_input,
                        vendor=supplier_val,
                        work_order_no=wo_no,
                        location=location
                    )
                    supplier = supplier_val or (vehicle.extra_data.get('owner_name', '') if (vehicle and isinstance(vehicle.extra_data, dict)) else None)

            # Get lists of items submitted (or fallback to single item fields)
            part_ids = request.POST.getlist('part_id[]') or request.POST.getlist('part_id')
            part_numbers = request.POST.getlist('part_number[]') or request.POST.getlist('part_number')
            part_names = request.POST.getlist('part_name[]') or request.POST.getlist('part_name')
            quantities = request.POST.getlist('quantity[]') or request.POST.getlist('quantity')
            rates = request.POST.getlist('rate[]') or request.POST.getlist('rate')
            reorder_levels = request.POST.getlist('reorder_level[]') or request.POST.getlist('reorder_level')
            remarks_list = request.POST.getlist('remarks[]') or request.POST.getlist('remarks')

            items_count = max(len(part_ids), len(part_numbers), len(part_names), len(quantities))
            if items_count == 0:
                return JsonResponse({'success': False, 'message': 'No parts provided.'})

            created_count = 0
            with transaction.atomic():
                for idx in range(items_count):
                    pid = part_ids[idx] if idx < len(part_ids) else None
                    pnum = part_numbers[idx] if idx < len(part_numbers) else ''
                    pname = part_names[idx] if idx < len(part_names) else ''
                    qty_raw = quantities[idx] if idx < len(quantities) else 0
                    rate_raw = rates[idx] if idx < len(rates) else 0
                    reorder_raw = reorder_levels[idx] if idx < len(reorder_levels) else 5
                    rem_raw = remarks_list[idx] if idx < len(remarks_list) else ''

                    try:
                        quantity = int(float(qty_raw or 0))
                    except Exception:
                        quantity = 0
                    try:
                        rate = float(rate_raw or 0)
                    except Exception:
                        rate = 0.0

                    if quantity <= 0:
                        continue

                    part = None
                    if pid:
                        part = SparePart.objects.filter(id=pid).first()
                    if not part and (pnum or pname):
                        part_number = pnum.strip() or f"PART-{int(timezone.now().timestamp()*1000)}"
                        part_name = pname.strip() or part_number
                        part, _ = SparePart.objects.get_or_create(
                            part_number=part_number,
                            defaults={'part_name': part_name, 'category': 'General'}
                        )
                    if not part:
                        continue

                    if reorder_raw:
                        try:
                            part.reorder_level = int(reorder_raw)
                        except Exception:
                            pass

                    if transaction_type == 'IN':
                        part.current_stock += quantity
                    else:
                        if part.current_stock < quantity:
                            return JsonResponse({'success': False, 'message': f'Not enough stock for {part.part_name} (Available: {part.current_stock}, Requested: {quantity}).'})
                        part.current_stock -= quantity

                    part.save()

                    # Manpower cost is applied to the first item of the transaction batch
                    mp_cost_for_item = manpower_cost if idx == 0 else 0.0

                    SparePartTransaction.objects.create(
                        part=part,
                        transaction_type=transaction_type,
                        date=date,
                        quantity=quantity,
                        rate=rate,
                        location=location,
                        wo_no=wo_no,
                        manpower_cost=mp_cost_for_item,
                        reference_no=reference_no,
                        vehicle=vehicle,
                        mechanic=mechanic,
                        supplier=supplier,
                        remarks=rem_raw,
                        entered_by=request.user
                    )
                    created_count += 1

            if created_count > 0:
                try:
                    messages.success(request, f'{created_count} Spare Part item(s) {transaction_type} recorded successfully.')
                    log_activity(request.user, 'CREATE', 'Spare Parts', f"Added {transaction_type} transaction ({created_count} item(s)) - WO: {wo_no or '-'}, Vehicle: {vehicle.regn if vehicle else '-'}", request)
                except Exception:
                    pass
                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'message': 'No valid part items to save.'})

        except Exception as e:
            import traceback
            traceback.print_exc()
            return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({'success': False, 'message': 'Invalid request.'})


def _group_spare_transactions(transactions):
    groups = []
    group_map = {}
    for t in transactions:
        v_key = t.vehicle_id if t.vehicle_id else (t.supplier or '').strip().lower()
        time_key = t.created_at.strftime('%Y-%m-%d %H:%M') if t.created_at else ''
        key = (
            time_key,
            t.date.strftime('%Y-%m-%d') if t.date else '',
            v_key,
            (t.wo_no or '').strip().lower(),
            (t.location or '').strip().lower(),
            (t.transaction_type or '').strip().upper()
        )
        if key not in group_map:
            group_map[key] = []
            groups.append(group_map[key])
        group_map[key].append(t)
    return groups


@login_required
def export_spare_parts(request):
    """Export transactions to excel formatted as Debit Note or custom flat layout"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from django.http import HttpResponse
    
    from .models import SparePartTransaction
    transactions = SparePartTransaction.objects.select_related('part', 'vehicle', 'entered_by').filter(is_deleted=False).order_by('date', 'id')
    
    # Apply same filters
    start_date = request.GET.get('start_date') or request.GET.get('date_from')
    end_date = request.GET.get('end_date') or request.GET.get('date_to')
    transaction_type = request.GET.get('transaction_type')
    vehicle_id = request.GET.get('vehicle_id')
    part_id = request.GET.get('part_id')
    vendor_name = request.GET.get('vendor_name') or request.GET.get('supplier')
    q = request.GET.get('q')
    
    if q:
        from django.db.models import Q
        transactions = transactions.filter(
            Q(part__part_name__icontains=q) |
            Q(part__part_number__icontains=q) |
            Q(vehicle__regn__icontains=q) |
            Q(supplier__icontains=q) |
            Q(location__icontains=q) |
            Q(wo_no__icontains=q)
        )
    
    if start_date:
        transactions = transactions.filter(date__gte=start_date)
    if end_date:
        transactions = transactions.filter(date__lte=end_date)
    if transaction_type:
        transactions = transactions.filter(transaction_type=transaction_type)
    if vehicle_id:
        transactions = transactions.filter(vehicle_id=vehicle_id)
    if part_id:
        transactions = transactions.filter(part_id=part_id)
    if vendor_name:
        transactions = transactions.filter(supplier=vendor_name)
        
    columns_param = request.GET.get('columns')
    
    # Standard columns mapping
    std_cols = [
        ('date', 'DATE', 12),
        ('location', 'LOCATION', 15),
        ('wo_no', 'WO NO', 20),
        ('vehicle', 'REG NO', 15),
        ('part_name', 'SPAREPARTS', 30),
        ('quantity', 'Qty', 12),
        ('rate', 'Cost of Spare Parts', 18),
        ('manpower_cost', 'Manpower cost', 15),
        ('total_amount', 'TOTAL AMOUNT', 15),
    ]
    # Extra columns mapping
    extra_cols = [
        ('reference_no', 'REF/INVOICE NO', 20),
        ('mechanic', 'MECHANIC', 15),
        ('entered_by', 'ENTERED BY', 15),
        ('remarks', 'REMARKS', 25),
    ]
    
    visible_cols = [('sr_no', 'SR.NO', 6)]
    
    if columns_param:
        selected_keys = [k.strip() for k in columns_param.split(',') if k.strip()]
    else:
        # Default keys for standard Debit Note
        selected_keys = ['date', 'location', 'wo_no', 'vehicle', 'part_name', 'part_number', 'quantity', 'rate', 'manpower_cost', 'total_amount', 'remarks']
        
    for key, label, width in std_cols:
        if key in selected_keys or (key == 'part_name' and 'part_number' in selected_keys):
            visible_cols.append((key, label, width))
            
    for key, label, width in extra_cols:
        if key in selected_keys:
            visible_cols.append((key, label, width))
            
    # If no columns are selected (unlikely), fallback
    if len(visible_cols) <= 1:
        visible_cols = [('sr_no', 'SR.NO', 6)] + [(k, l, w) for k, l, w in std_cols]
        
    N = len(visible_cols)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="Debit_Note_SpareParts.xlsx"'
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spare Parts"
    
    border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    
    # Dynamic Merging for Header (1 to N-1)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 25
    if N > 2:
        last_letter = openpyxl.utils.get_column_letter(N)
        prev_letter = openpyxl.utils.get_column_letter(N - 1)
        
        # Row 1: Project Name
        ws.merge_cells(f'A1:{prev_letter}1')
        c1 = ws.cell(row=1, column=1, value="Proposed International Airport Project in Gelephu")
        c1.font = Font(bold=True, size=12)
        c1.alignment = Alignment(horizontal='center', vertical='center')
        
        # Row 2: Debit Note Title
        ws.merge_cells(f'A2:{prev_letter}2')
        sub_title = "DEBIT NOTE FOR SPAREPARTS"
        if vendor_name:
            sub_title += f" - {vendor_name}"
        c2 = ws.cell(row=2, column=1, value=sub_title)
        c2.font = Font(bold=True, size=13)
        c2.alignment = Alignment(horizontal='center', vertical='center')
        
        # Logo cell at N (Rows 1-2 merged)
        ws.merge_cells(f'{last_letter}1:{last_letter}2')
        logo = _tyre_logo_image(width=52, height=52)
        if logo is not None:
            ws.add_image(logo, f'{last_letter}1')
        else:
            cl = ws.cell(row=1, column=N)
            cl.value = "RIGSAR - VAJRA"
            cl.font = Font(bold=True, size=9)
            cl.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    else:
        # Simple row headers if too narrow
        ws.cell(row=1, column=1, value="Proposed International Airport Project").font = Font(bold=True)
        
    # Headers row
    for col_num, (key, label, width) in enumerate(visible_cols, 1):
        cell = ws.cell(row=3, column=col_num, value=label)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = border
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = width
        
    grouped_txs = _group_spare_transactions(transactions)
    row_num = 4
    sum_part_cost = 0
    sum_manpower = 0
    sum_total = 0
    
    for i, t_list in enumerate(grouped_txs, 1):
        grp_part_cost = sum(t.quantity * t.rate for t in t_list)
        grp_manpower = sum(t.manpower_cost for t in t_list)
        grp_total = sum(t.total_amount for t in t_list)
        
        sum_part_cost += grp_part_cost
        sum_manpower += grp_manpower
        sum_total += grp_total
        
        first_t = t_list[0]
        for col_idx, (key, label, width) in enumerate(visible_cols, 1):
            cell = ws.cell(row=row_num, column=col_idx)
            cell.border = border
            cell.alignment = Alignment(vertical='center', wrap_text=True)
            
            if key == 'sr_no':
                cell.value = i
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'date':
                cell.value = first_t.date.strftime('%d.%m.%Y') if first_t.date else ''
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'location':
                cell.value = first_t.location or "Gelephu"
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'wo_no':
                cell.value = first_t.wo_no or "-"
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'vehicle':
                cell.value = first_t.vehicle.regn if first_t.vehicle else (first_t.supplier or "-")
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'part_name':
                if len(t_list) == 1:
                    t = t_list[0]
                    pname = t.part.part_name if t.part else ""
                    pnum = t.part.part_number if t.part else ""
                    cell.value = f"{pname} ({pnum})" if ('part_number' in selected_keys and pnum) else pname
                else:
                    items_str = []
                    for idx, t in enumerate(t_list, 1):
                        pname = t.part.part_name if t.part else ""
                        pnum = t.part.part_number if t.part else ""
                        p_desc = f"{pname} ({pnum})" if ('part_number' in selected_keys and pnum) else pname
                        items_str.append(f"{idx}. {p_desc}")
                    cell.value = "\n".join(items_str)
            elif key == 'quantity':
                if len(t_list) == 1:
                    t = t_list[0]
                    unit = "Nos" if (t.part and ("No" in t.part.unit or "no" in t.part.unit)) else (t.part.unit if t.part else "")
                    cell.value = f"{t.quantity} {unit}"
                else:
                    q_str = []
                    for t in t_list:
                        unit = "Nos" if (t.part and ("No" in t.part.unit or "no" in t.part.unit)) else (t.part.unit if t.part else "")
                        q_str.append(f"{t.quantity} {unit}")
                    cell.value = "\n".join(q_str)
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            elif key == 'rate':
                if len(t_list) == 1:
                    cell.value = float(grp_part_cost)
                else:
                    r_str = [f"Nu. {t.quantity * t.rate:,.2f}" for t in t_list]
                    cell.value = "\n".join(r_str)
            elif key == 'manpower_cost':
                cell.value = float(grp_manpower)
            elif key == 'total_amount':
                cell.value = float(grp_total)
            elif key == 'reference_no':
                refs = list(dict.fromkeys([t.reference_no for t in t_list if t.reference_no]))
                cell.value = ", ".join(refs) if refs else "-"
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'mechanic':
                mechs = list(dict.fromkeys([t.mechanic.name if (t.mechanic and hasattr(t.mechanic, 'name')) else str(t.mechanic) for t in t_list if t.mechanic]))
                cell.value = ", ".join(mechs) if mechs else "-"
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'entered_by':
                eb = first_t.entered_by
                cell.value = eb.full_name or eb.username if eb else "-"
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif key == 'remarks':
                rems = [t.remarks for t in t_list if t.remarks]
                cell.value = "\n".join(rems) if rems else "-"
        row_num += 1

    # Total Row Calculation
    rate_col_idx = None
    mp_col_idx = None
    tot_col_idx = None
    
    for idx, (key, _, _) in enumerate(visible_cols, 1):
        if key == 'rate':
            rate_col_idx = idx
        elif key == 'manpower_cost':
            mp_col_idx = idx
        elif key == 'total_amount':
            tot_col_idx = idx
            
    num_indices = [idx for idx in [rate_col_idx, mp_col_idx, tot_col_idx] if idx is not None]
    if num_indices:
        first_num_idx = min(num_indices)
        if first_num_idx > 1:
            ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=first_num_idx - 1)
            tot_cell = ws.cell(row=row_num, column=1, value="TOTAL AMOUNT")
            tot_cell.font = Font(bold=True)
            tot_cell.alignment = Alignment(horizontal='right')
            
        for col_idx in range(1, len(visible_cols) + 1):
            ws.cell(row=row_num, column=col_idx).border = border
            
        if rate_col_idx:
            c = ws.cell(row=row_num, column=rate_col_idx, value=float(sum_part_cost))
            c.font = Font(bold=True)
        if mp_col_idx:
            c = ws.cell(row=row_num, column=mp_col_idx, value=float(sum_manpower))
            c.font = Font(bold=True)
        if tot_col_idx:
            c = ws.cell(row=row_num, column=tot_col_idx, value=float(sum_total))
            c.font = Font(bold=True)
        row_num += 1
        
    # Dynamic Signature Row
    row_num += 1
    ws.row_dimensions[row_num].height = 60
    
    width_per_sig = max(1, len(visible_cols) // 5)
    vendor_name_sig = vendor_name or ""
    sig_titles = [
        "PREPARED BY",
        "P&M MANAGER",
        "PROJECT MANAGER",
        "PROJECT DIRECTOR",
        f"ACCEPTED BY {vendor_name_sig.upper()}" if vendor_name_sig else "ACCEPTED BY"
    ]
    
    start_c = 1
    for sig_idx, title in enumerate(sig_titles):
        end_c = start_c + width_per_sig - 1
        if sig_idx == len(sig_titles) - 1:
            end_c = len(visible_cols)
            
        if end_c >= start_c:
            ws.merge_cells(start_row=row_num, start_column=start_c, end_row=row_num, end_column=end_c)
            sig_cell = ws.cell(row=row_num, column=start_c, value=title)
            sig_cell.alignment = Alignment(horizontal='center', vertical='bottom')
            sig_cell.font = Font(bold=True, size=9)
            for col_i in range(start_c, end_c + 1):
                ws.cell(row=row_num, column=col_i).border = border
        start_c = end_c + 1
        
    # Outer Border fix
    for r in ws.iter_rows(min_row=1, max_row=row_num, min_col=1, max_col=len(visible_cols)):
        for c in r:
            if not c.border:
                c.border = border
                
    wb.save(response)
    return response

@login_required
def export_spare_parts_pdf(request):
    """HTML print view for PDF export"""
    from django.shortcuts import render
    from django.http import HttpResponse
    from django.utils import timezone
    from .models import SparePartTransaction
    vendor_name = request.GET.get('vendor_name', '')
    vendor_filter = request.GET.get('vendor_name') or request.GET.get('supplier')
    
    transactions = SparePartTransaction.objects.select_related('part', 'vehicle', 'entered_by').filter(is_deleted=False).order_by('date', 'id')
    
    # Apply same filters
    start_date = request.GET.get('start_date') or request.GET.get('date_from')
    end_date = request.GET.get('end_date') or request.GET.get('date_to')
    transaction_type = request.GET.get('transaction_type')
    vehicle_id = request.GET.get('vehicle_id')
    part_id = request.GET.get('part_id')
    q = request.GET.get('q')
    
    if q:
        from django.db.models import Q
        transactions = transactions.filter(
            Q(part__part_name__icontains=q) |
            Q(part__part_number__icontains=q) |
            Q(vehicle__regn__icontains=q) |
            Q(supplier__icontains=q) |
            Q(location__icontains=q) |
            Q(wo_no__icontains=q)
        )
    
    if start_date:
        transactions = transactions.filter(date__gte=start_date)
    if end_date:
        transactions = transactions.filter(date__lte=end_date)
    if transaction_type:
        transactions = transactions.filter(transaction_type=transaction_type)
    if vehicle_id:
        transactions = transactions.filter(vehicle_id=vehicle_id)
    if part_id:
        transactions = transactions.filter(part_id=part_id)
    if vendor_filter:
        transactions = transactions.filter(supplier=vendor_filter)
        
    columns_param = request.GET.get('columns')
    
    # Standard columns mapping
    std_cols = [
        ('date', 'DATE', 12),
        ('location', 'LOCATION', 15),
        ('wo_no', 'WO NO', 20),
        ('vehicle', 'REG NO', 15),
        ('part_name', 'SPAREPARTS', 30),
        ('quantity', 'Qty', 8),
        ('rate', 'Cost of Spare Parts', 15),
        ('manpower_cost', 'Manpower cost', 15),
        ('total_amount', 'TOTAL AMOUNT', 15),
    ]
    # Extra columns mapping
    extra_cols = [
        ('reference_no', 'REF/INVOICE NO', 20),
        ('mechanic', 'MECHANIC', 15),
        ('entered_by', 'ENTERED BY', 15),
        ('remarks', 'REMARKS', 25),
    ]
    
    visible_cols = [('sr_no', 'SR.NO', 6)]
    
    if columns_param:
        selected_keys = [k.strip() for k in columns_param.split(',') if k.strip()]
    else:
        # Default keys for standard Debit Note
        selected_keys = ['date', 'location', 'wo_no', 'vehicle', 'part_name', 'part_number', 'quantity', 'rate', 'manpower_cost', 'total_amount', 'remarks']
        
    for key, label, width in std_cols:
        if key in selected_keys or (key == 'part_name' and 'part_number' in selected_keys):
            visible_cols.append((key, label, width))
            
    for key, label, width in extra_cols:
        if key in selected_keys:
            visible_cols.append((key, label, width))
            
    # If no columns are selected (unlikely), fallback
    if len(visible_cols) <= 1:
        visible_cols = [('sr_no', 'SR.NO', 6)] + [(k, l, w) for k, l, w in std_cols]
        
    # Build dynamic row data cells
    grouped_txs = _group_spare_transactions(transactions)
    rows = []
    sum_part_cost = 0
    sum_manpower = 0
    sum_total = 0
    
    for i, t_list in enumerate(grouped_txs, 1):
        grp_part_cost = sum(t.quantity * t.rate for t in t_list)
        grp_manpower = sum(t.manpower_cost for t in t_list)
        grp_total = sum(t.total_amount for t in t_list)
        
        sum_part_cost += grp_part_cost
        sum_manpower += grp_manpower
        sum_total += grp_total
        
        first_t = t_list[0]
        row_cells = []
        for col_idx, (key, label, width) in enumerate(visible_cols, 1):
            if key == 'sr_no':
                val = i
            elif key == 'date':
                val = first_t.date.strftime('%d.%m.%Y') if first_t.date else ''
            elif key == 'location':
                val = first_t.location or "Gelephu"
            elif key == 'wo_no':
                val = first_t.wo_no or "-"
            elif key == 'vehicle':
                val = first_t.vehicle.regn if first_t.vehicle else (first_t.supplier or "-")
            elif key == 'part_name':
                if len(t_list) == 1:
                    t = t_list[0]
                    pname = t.part.part_name if t.part else ""
                    pnum = t.part.part_number if t.part else ""
                    val = f"{pname} ({pnum})" if ('part_number' in selected_keys and pnum) else pname
                else:
                    items_str = []
                    for idx, t in enumerate(t_list, 1):
                        pname = t.part.part_name if t.part else ""
                        pnum = t.part.part_number if t.part else ""
                        p_desc = f"{pname} ({pnum})" if ('part_number' in selected_keys and pnum) else pname
                        items_str.append(f"{idx}. {p_desc}")
                    val = "<br>".join(items_str)
            elif key == 'quantity':
                if len(t_list) == 1:
                    t = t_list[0]
                    unit = "Nos" if (t.part and ("No" in t.part.unit or "no" in t.part.unit)) else (t.part.unit if t.part else "")
                    val = f"{t.quantity} {unit}"
                else:
                    q_str = []
                    for t in t_list:
                        unit = "Nos" if (t.part and ("No" in t.part.unit or "no" in t.part.unit)) else (t.part.unit if t.part else "")
                        q_str.append(f"{t.quantity} {unit}")
                    val = "<br>".join(q_str)
            elif key == 'rate':
                if len(t_list) == 1:
                    val = f"Nu. {grp_part_cost:,.2f}"
                else:
                    r_str = [f"Nu. {t.quantity * t.rate:,.2f}" for t in t_list]
                    val = "<br>".join(r_str)
            elif key == 'manpower_cost':
                val = f"Nu. {grp_manpower:,.2f}"
            elif key == 'total_amount':
                val = f"Nu. {grp_total:,.2f}"
            elif key == 'reference_no':
                refs = list(dict.fromkeys([t.reference_no for t in t_list if t.reference_no]))
                val = ", ".join(refs) if refs else "-"
            elif key == 'mechanic':
                mechs = list(dict.fromkeys([t.mechanic.name if (t.mechanic and hasattr(t.mechanic, 'name')) else str(t.mechanic) for t in t_list if t.mechanic]))
                val = ", ".join(mechs) if mechs else "-"
            elif key == 'entered_by':
                eb = first_t.entered_by
                val = eb.full_name or eb.username if eb else "-"
            elif key == 'remarks':
                rems = [t.remarks for t in t_list if t.remarks]
                val = "<br>".join(rems) if rems else "-"
            else:
                val = ""
            row_cells.append(val)
        rows.append(row_cells)
        
    # Calculate colspan and totals cells
    rate_col_idx = None
    mp_col_idx = None
    tot_col_idx = None
    
    for idx, (key, _, _) in enumerate(visible_cols):
        if key == 'rate':
            rate_col_idx = idx
        elif key == 'manpower_cost':
            mp_col_idx = idx
        elif key == 'total_amount':
            tot_col_idx = idx
            
    num_indices = [idx for idx in [rate_col_idx, mp_col_idx, tot_col_idx] if idx is not None]
    
    colspan = 0
    total_cells = []
    if num_indices:
        first_num_idx = min(num_indices)
        colspan = first_num_idx
        for idx in range(colspan, len(visible_cols)):
            key = visible_cols[idx][0]
            if key == 'rate':
                total_cells.append(f"Nu. {sum_part_cost:,.2f}")
            elif key == 'manpower_cost':
                total_cells.append(f"Nu. {sum_manpower:,.2f}")
            elif key == 'total_amount':
                total_cells.append(f"Nu. {sum_total:,.2f}")
            else:
                total_cells.append("")
            
    context = {
        'visible_cols': visible_cols,
        'rows': rows,
        'colspan': colspan,
        'total_cells': total_cells,
        'vendor_name': vendor_name,
        'start_date': start_date,
        'end_date': end_date,
        'transaction_type': transaction_type,
        'q': q,
    }
    return render(request, 'fleet/debit_note_print.html', context)




@login_required
def api_delete_spare_part_transaction(request, tx_id):
    """Soft-delete a transaction and revert stock changes."""
    if request.method == 'POST':
        from .models import SparePartTransaction
        from django.shortcuts import get_object_or_404
        from django.utils import timezone
        tx = get_object_or_404(SparePartTransaction, id=tx_id)
        part = tx.part
        
        # Revert stock change
        if tx.transaction_type == 'IN':
            part.current_stock -= tx.quantity
        else:
            part.current_stock += tx.quantity
        
        part.save()
        # Soft delete instead of hard delete
        tx.is_deleted = True
        tx.deleted_at = timezone.now()
        tx.deleted_by = request.user
        tx.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
        log_activity(request.user, 'DELETE', 'Spare Parts', f"Deleted Spare Part transaction #{tx_id} ({tx.transaction_type}, {tx.quantity} x {part.part_name})", request)
        return JsonResponse({'success': True, 'message': 'Entry deleted. Restore from Admin Panel.'})
    return JsonResponse({'success': False, 'message': 'Invalid request method.'})


@login_required
def api_approve_spare_part_transaction(request, tx_id):
    """Approve a transaction."""
    if request.method == 'POST':
        from .models import SparePartTransaction
        from django.shortcuts import get_object_or_404
        tx = get_object_or_404(SparePartTransaction, id=tx_id)
        
        tx.is_approved = True
        tx.approved_by = request.user
        tx.save()
        log_activity(request.user, 'APPROVE', 'Spare Parts', f"Approved Spare Part transaction #{tx_id} ({tx.transaction_type}, {tx.quantity} x {tx.part.part_name})", request)
        return JsonResponse({
            'success': True,
            'approved_by': tx.approved_by.full_name or tx.approved_by.username
        })
    return JsonResponse({'success': False, 'message': 'Invalid request method.'})



@login_required
def api_edit_spare_part_transaction(request, tx_id):
    """Edit transaction details and adjust stock accordingly."""
    if request.method == 'POST':
        from .models import SparePartTransaction, FleetVehicle
        from django.shortcuts import get_object_or_404
        tx = get_object_or_404(SparePartTransaction, id=tx_id)
        part = tx.part
        
        old_qty = tx.quantity
        old_type = tx.transaction_type
        
        # Read new details
        new_qty = int(request.POST.get('quantity', old_qty))
        new_rate = float(request.POST.get('rate', tx.rate))
        new_manpower = float(request.POST.get('manpower_cost', tx.manpower_cost or 0))
        
        # Update fields
        tx.date = request.POST.get('date', tx.date)
        tx.location = request.POST.get('location', tx.location)
        tx.wo_no = request.POST.get('wo_no', tx.wo_no)
        tx.remarks = request.POST.get('remarks', tx.remarks)
        tx.supplier = request.POST.get('supplier', tx.supplier)
        tx.quantity = new_qty
        tx.rate = new_rate
        tx.manpower_cost = new_manpower
        
        # Adjust stock
        # 1. Revert old change
        if old_type == 'IN':
            part.current_stock -= old_qty
        else:
            part.current_stock += old_qty
            
        # 2. Apply new change
        if old_type == 'IN':
            part.current_stock += new_qty
        else:
            part.current_stock -= new_qty
            
        part.save()
        tx.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'message': 'Invalid request method.'})


@login_required
def export_vehicles_excel(request):
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from django.http import HttpResponse
    from django.db.models import Q
    from .models import FleetVehicle, HiredVehicle, RepairLog
    from django.utils.timezone import localtime
    import datetime

    # Get filters
    q = request.GET.get('search', '').strip() or request.GET.get('q', '').strip()
    dept = request.GET.get('department', '').strip()
    status = request.GET.get('status', '').strip()
    ownership = request.GET.get('ownership', '').strip()
    report_type = request.GET.get('report_type', 'fleet').strip().lower()
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()

    title_font = Font(name='Segoe UI', size=16, bold=True, color='1E3A8A')
    header_font = Font(name='Segoe UI', size=10, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1E3A8A', end_color='1E3A8A', fill_type='solid')
    align_center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    align_left = Alignment(horizontal='left', vertical='center')
    thin_border = Border(
        left=Side(style='thin', color='E5E7EB'),
        right=Side(style='thin', color='E5E7EB'),
        top=Side(style='thin', color='E5E7EB'),
        bottom=Side(style='thin', color='E5E7EB')
    )

    # LIFECYCLE REPORT: In Date, Complaint, Garage, Mechanic, Parts, Out Date, Downtime, Status in one sheet
    if report_type == 'lifecycle':
        rep_qs = RepairLog.objects.select_related('vehicle').order_by('-in_date', '-id')
        if date_from_str:
            try:
                df = datetime.datetime.strptime(date_from_str, '%Y-%m-%d').date()
                rep_qs = rep_qs.filter(in_date__gte=df)
            except ValueError:
                pass
        if date_to_str:
            try:
                dt = datetime.datetime.strptime(date_to_str, '%Y-%m-%d').date()
                rep_qs = rep_qs.filter(in_date__lte=dt)
            except ValueError:
                pass
        if status and status != 'ALL':
            s_low = status.lower()
            if s_low in ['workshop', 'active']:
                rep_qs = rep_qs.filter(out_date__isnull=True)
            elif s_low in ['completed', 'released', 'repaired']:
                rep_qs = rep_qs.filter(out_date__isnull=False)
        if ownership and ownership != 'ALL':
            if ownership.lower() == 'hired':
                rep_qs = rep_qs.filter(vehicle__extra_data__ownership__iexact='hired')
            else:
                rep_qs = rep_qs.exclude(vehicle__extra_data__ownership__iexact='hired')
        if q:
            rep_qs = rep_qs.filter(
                Q(vehicle__dno__icontains=q) |
                Q(vehicle__regn__icontains=q) |
                Q(complaint__icontains=q) |
                Q(mechanic__icontains=q)
            )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Breakdown Lifecycle"
        ws.views.sheetView[0].showGridLines = True

        ws.merge_cells('A1:P1')
        ws['A1'] = "Vehicle Breakdown & Workshop Lifecycle Report"
        ws['A1'].font = title_font
        ws['A1'].alignment = Alignment(horizontal='left', vertical='center')
        ws.row_dimensions[1].height = 28

        ws.merge_cells('A2:P2')
        filter_parts = [f"Export Date: {localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')}"]
        if date_from_str or date_to_str:
            filter_parts.append(f"Period: {date_from_str or 'Start'} to {date_to_str or 'Current'}")
        if status and status != 'ALL':
            filter_parts.append(f"Status: {status}")
        if ownership and ownership != 'ALL':
            filter_parts.append(f"Type: {ownership}")
        if q:
            filter_parts.append(f"Search: {q}")
        ws['A2'] = " | ".join(filter_parts)
        ws['A2'].font = Font(name='Segoe UI', size=9, italic=True, color='4B5563')
        ws['A2'].alignment = Alignment(horizontal='left', vertical='center')
        ws.row_dimensions[2].height = 20

        ws.row_dimensions[4].height = 25
        headers = [
            "SL", "VEHICLE (DOOR NO)", "REGN NO", "MODEL NAME", "OWNERSHIP",
            "IN DATE", "IN TIME", "COMPLAINT / ISSUE", "GARAGE / WORKSHOP",
            "MECHANIC", "PARTS REPLACED", "OUT DATE (RELEASED)", "OUT TIME",
            "DOWNTIME (DAYS)", "STATUS", "REMARKS"
        ]
        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=4, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = align_center
            cell.border = thin_border

        today = datetime.date.today()
        row_num = 5
        for idx, r in enumerate(rep_qs, start=1):
            ws.row_dimensions[row_num].height = 20
            v = r.vehicle
            v_own = (v.extra_data.get('ownership') if v and isinstance(v.extra_data, dict) else 'In-house') or 'In-house'
            
            if r.out_date and r.in_date:
                dt_days = max(1, (r.out_date - r.in_date).days)
                stat_text = "Repaired"
            elif r.in_date:
                dt_days = (today - r.in_date).days
                stat_text = "In Workshop"
            else:
                dt_days = 0
                stat_text = "In Workshop"

            parts_str = r.parts_used or '-'
            if r.qty and parts_str != '-':
                parts_str += f" ({r.qty})"

            garage = (r.extra_data.get('garage_name') if isinstance(r.extra_data, dict) else 'Main Yard Workshop') or 'Main Yard Workshop'

            row_data = [
                idx,
                v.dno if v else '-',
                v.regn if v else '-',
                v.model_name if v else '-',
                v_own,
                r.in_date.strftime('%d-%m-%Y') if r.in_date else '-',
                r.in_time.strftime('%H:%M') if r.in_time else '-',
                r.complaint or '-',
                garage,
                r.mechanic or '-',
                parts_str,
                r.out_date.strftime('%d-%m-%Y') if r.out_date else '-',
                r.out_time.strftime('%H:%M') if r.out_time else '-',
                dt_days,
                stat_text,
                r.remarks or '-'
            ]
            for col_num, val in enumerate(row_data, start=1):
                cell = ws.cell(row=row_num, column=col_num, value=val)
                cell.border = thin_border
                cell.font = Font(name='Segoe UI', size=9)
                if col_num in [8, 11, 16]:
                    cell.alignment = align_left
                else:
                    cell.alignment = align_center

            if row_num % 2 == 0:
                row_fill = PatternFill(start_color='F9FAFB', end_color='F9FAFB', fill_type='solid')
                for cell in ws[row_num]:
                    if cell.column <= len(headers):
                        cell.fill = row_fill
            row_num += 1

        for col in ws.columns:
            if col[0].column > len(headers):
                continue
            max_len = 0
            for cell in col:
                val_str = str(cell.value or '')
                if len(val_str) > max_len:
                    max_len = len(val_str)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = "attachment; filename=Breakdown_Lifecycle_Report.xlsx"
        wb.save(response)
        return response

    # STANDARD MASTER FLEET DIRECTORY REPORT
    queryset = FleetVehicle.objects.all().order_by('regn')

    # Apply filters
    if q:
        queryset = queryset.filter(
            Q(dno__icontains=q) |
            Q(regn__icontains=q) |
            Q(model_name__icontains=q)
        )
    if dept and dept != 'ALL':
        queryset = queryset.filter(department__name=dept)
    if status and status != 'ALL':
        queryset = queryset.filter(
            Q(extra_data__vehicle_status__iexact=status) |
            Q(extra_data__status__iexact=status)
        )
    if ownership and ownership != 'ALL':
        queryset = queryset.filter(extra_data__ownership__iexact=ownership)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Master Fleet List"
    ws.views.sheetView[0].showGridLines = True

    # Title info
    ws.merge_cells('A1:I1')
    ws['A1'] = "Master Fleet (P&M Vehicles) Report"
    ws['A1'].font = title_font
    ws['A1'].alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28

    ws.merge_cells('A2:I2')
    filter_desc = f"Export Date: {localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')}"
    if q:
        filter_desc += f" | Search: {q}"
    if dept and dept != 'ALL':
        filter_desc += f" | Dept: {dept}"
    if status and status != 'ALL':
        filter_desc += f" | Status: {status}"
    ws['A2'] = filter_desc
    ws['A2'].font = Font(name='Segoe UI', size=9, italic=True, color='4B5563')
    ws['A2'].alignment = Alignment(horizontal='left', vertical='center')

    # Logo in J1:J2
    ws.merge_cells('J1:J2')
    logo = _tyre_logo_image(width=52, height=52)
    if logo is not None:
        ws.add_image(logo, 'J1')
    ws.row_dimensions[2].height = 20

    # Headers
    ws.row_dimensions[4].height = 25
    headers = [
        "REG NO / ID", "DOOR NO", "MODEL NAME", "OWNERSHIP", 
        "OWNER / VENDOR", "AGREEMENT REF / WO", "LOCATION", 
        "DEPARTMENT", "STATUS", "REMARKS"
    ]

    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center
        cell.border = thin_border

    # Data
    row_num = 5
    for v in queryset:
        ws.row_dimensions[row_num].height = 20
        ownership_val = v.extra_data.get('ownership', 'In-house')
        owner_name = v.extra_data.get('owner_name', '-')
        contract_ref = v.extra_data.get('contract_ref', '-')
        location = v.extra_data.get('location', '-')
        status_val = v.extra_data.get('vehicle_status', v.extra_data.get('status', 'Running'))

        if ownership_val == 'Hired' and (not owner_name or not contract_ref):
            hv = HiredVehicle.objects.filter(regn__iexact=v.regn).first()
            if hv:
                owner_name = owner_name or hv.owner_name
                contract_ref = contract_ref or hv.agreement_ref

        data = [
            v.regn or '-',
            v.dno or '-',
            v.model_name or '-',
            ownership_val,
            owner_name or '-',
            contract_ref or '-',
            location or '-',
            v.department.name if v.department else '-',
            status_val or 'Running',
            v.remarks or '-'
        ]

        for col_num, val in enumerate(data, start=1):
            cell = ws.cell(row=row_num, column=col_num, value=val)
            cell.border = thin_border
            cell.font = Font(name='Segoe UI', size=9)
            if col_num in [3, 5, 6, 10]:
                cell.alignment = align_left
            else:
                cell.alignment = align_center

        if row_num % 2 == 0:
            row_fill = PatternFill(start_color='F9FAFB', end_color='F9FAFB', fill_type='solid')
            for cell in ws[row_num]:
                if cell.column <= 10:
                    cell.fill = row_fill

        row_num += 1

    # Widths
    for col in ws.columns:
        if col[0].column > 10:
            continue
        max_len = 0
        for cell in col:
            val_str = str(cell.value or '')
            if len(val_str) > max_len:
                max_len = len(val_str)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = "attachment; filename=Master_Fleet_Report.xlsx"
    wb.save(response)
    return response


@login_required
def export_vehicles_pdf(request):
    from django.shortcuts import render
    from django.db.models import Q
    from .models import FleetVehicle, HiredVehicle, RepairLog
    from django.utils.timezone import localtime
    import datetime

    # Get filters
    q = request.GET.get('search', '').strip() or request.GET.get('q', '').strip()
    dept = request.GET.get('department', '').strip()
    status = request.GET.get('status', '').strip()
    ownership = request.GET.get('ownership', '').strip()
    report_type = request.GET.get('report_type', 'fleet').strip().lower()
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()

    # LIFECYCLE PDF REPORT
    if report_type == 'lifecycle':
        today = datetime.date.today()
        rep_qs = RepairLog.objects.select_related('vehicle').order_by('-in_date', '-id')
        if date_from_str:
            try:
                df = datetime.datetime.strptime(date_from_str, '%Y-%m-%d').date()
                rep_qs = rep_qs.filter(in_date__gte=df)
            except ValueError:
                pass
        if date_to_str:
            try:
                dt = datetime.datetime.strptime(date_to_str, '%Y-%m-%d').date()
                rep_qs = rep_qs.filter(in_date__lte=dt)
            except ValueError:
                pass
        if status and status != 'ALL':
            s_low = status.lower()
            if s_low in ['workshop', 'active']:
                rep_qs = rep_qs.filter(out_date__isnull=True)
            elif s_low in ['completed', 'released', 'repaired']:
                rep_qs = rep_qs.filter(out_date__isnull=False)
        if ownership and ownership != 'ALL':
            if ownership.lower() == 'hired':
                rep_qs = rep_qs.filter(vehicle__extra_data__ownership__iexact='hired')
            else:
                rep_qs = rep_qs.exclude(vehicle__extra_data__ownership__iexact='hired')
        if q:
            rep_qs = rep_qs.filter(
                Q(vehicle__dno__icontains=q) |
                Q(vehicle__regn__icontains=q) |
                Q(complaint__icontains=q) |
                Q(mechanic__icontains=q)
            )

        records = list(rep_qs)
        active_c = 0
        completed_c = 0
        total_dt = 0
        for r in records:
            if r.out_date and r.in_date:
                r.downtime_days = max(1, (r.out_date - r.in_date).days)
                completed_c += 1
                total_dt += r.downtime_days
            elif r.in_date:
                r.downtime_days = max(0, (today - r.in_date).days)
                active_c += 1
                total_dt += r.downtime_days
            else:
                r.downtime_days = 0
                active_c += 1

        filter_parts = []
        if date_from_str or date_to_str:
            filter_parts.append(f"Period: {date_from_str or 'Start'} to {date_to_str or 'Current'}")
        if status and status != 'ALL':
            filter_parts.append(f"Status: {status}")
        if ownership and ownership != 'ALL':
            filter_parts.append(f"Type: {ownership}")
        if q:
            filter_parts.append(f"Search: {q}")

        return render(request, 'fleet/breakdown_lifecycle_print.html', {
            'records': records,
            'active_count': active_c,
            'completed_count': completed_c,
            'total_downtime_days': total_dt,
            'filter_desc': " | ".join(filter_parts),
            'export_time': localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')
        })

    # STANDARD VEHICLE DIRECTORY PRINT
    queryset = FleetVehicle.objects.all().order_by('regn')

    # Apply filters
    if q:
        queryset = queryset.filter(
            Q(dno__icontains=q) |
            Q(regn__icontains=q) |
            Q(model_name__icontains=q)
        )
    if dept and dept != 'ALL':
        queryset = queryset.filter(department__name=dept)
    if status and status != 'ALL':
        queryset = queryset.filter(
            Q(extra_data__vehicle_status__iexact=status) |
            Q(extra_data__status__iexact=status)
        )
    if ownership and ownership != 'ALL':
        queryset = queryset.filter(extra_data__ownership__iexact=ownership)

    # Attach dynamic properties for printing
    for v in queryset:
        v.ownership_type = v.extra_data.get('ownership', 'In-house')
        v.owner_val = v.extra_data.get('owner_name', '-')
        v.contract_val = v.extra_data.get('contract_ref', '-')
        v.loc_val = v.extra_data.get('location', '-')
        v.status_val = v.extra_data.get('vehicle_status', v.extra_data.get('status', 'Running'))
        
        if v.ownership_type == 'Hired' and (not v.owner_val or not v.contract_val):
            hv = HiredVehicle.objects.filter(regn__iexact=v.regn).first()
            if hv:
                v.owner_val = v.owner_val or hv.owner_name
                v.contract_val = v.contract_val or hv.agreement_ref

    filter_desc = ""
    if q:
        filter_desc += f"Search: {q}"
    if dept and dept != 'ALL':
        filter_desc += f" | Dept: {dept}"
    if status and status != 'ALL':
        filter_desc += f" | Status: {status}"
    if ownership and ownership != 'ALL':
        filter_desc += f" | Type: {ownership}"

    return render(request, 'fleet/vehicles_print.html', {
        'vehicles': queryset,
        'filter_desc': filter_desc,
        'export_time': localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')
    })


@login_required
def api_vehicles_lookup(request):
    from django.http import JsonResponse
    from .models import FleetVehicle, HiredVehicle
    import re
    
    data = []
    known_regs = set()
    # Fetch from FleetVehicle (both in-house and hired)
    for v in FleetVehicle.objects.all():
        ownership = v.extra_data.get('ownership', 'In-house') if isinstance(v.extra_data, dict) else 'In-house'
        owner_name = v.extra_data.get('owner_name', '') if isinstance(v.extra_data, dict) else ''
        contract_ref = v.extra_data.get('contract_ref', '') if isinstance(v.extra_data, dict) else ''
        location = v.extra_data.get('location', '') if isinstance(v.extra_data, dict) else ''
        
        # If hired but owner_name/agreement_ref is missing in extra_data, try matching with HiredVehicle model
        if not owner_name or not contract_ref:
            norm_reg = re.sub(r'[^A-Z0-9]', '', (v.regn or '').upper())
            hvs = HiredVehicle.objects.all()
            hv = next((x for x in hvs if re.sub(r'[^A-Z0-9]', '', (x.regn or '').upper()) == norm_reg), None)
            if hv:
                owner_name = owner_name or hv.owner_name or ''
                contract_ref = contract_ref or hv.agreement_ref or ''
                
        reg_val = v.regn or v.dno
        clean_reg = re.sub(r'[^A-Z0-9]', '', reg_val.upper())
        if clean_reg:
            known_regs.add(clean_reg)
            
        data.append({
            'id': v.id,
            'regn': reg_val,
            'dno': v.dno,
            'ownership': ownership,
            'owner_name': owner_name,
            'contract_ref': contract_ref,
            'location': location
        })
        
    # Also add HiredVehicle entries not yet in FleetVehicle
    for hv in HiredVehicle.objects.all():
        c_reg = re.sub(r'[^A-Z0-9]', '', (hv.regn or '').upper())
        if c_reg and c_reg not in known_regs:
            data.append({
                'id': 0,
                'regn': hv.regn,
                'dno': hv.regn,
                'ownership': 'Hired',
                'owner_name': hv.owner_name or '',
                'contract_ref': hv.agreement_ref or '',
                'location': 'Gelephu'
            })
            known_regs.add(c_reg)
            
    return JsonResponse(data, safe=False)
