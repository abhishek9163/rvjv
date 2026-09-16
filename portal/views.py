import urllib.request
import urllib.parse
import base64
import json
import re
from collections import Counter, defaultdict
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from .models import DailyDeployment, User, CompanyPost, Vehicle, Employee, EmployeeDocument, VehicleDocument, InsuranceDocument, SystemSettings, Notification, UserActivityLog, OvertimeRecord, EmployeeAttendance, MessLog, MessGuestCoupon, MessMenu, MessFoodStatus, MessFeedback, MessLocation, MessWastageLog, CampMovementLog
from fleet.models import FleetVehicle, RepairLog, ServiceLog
import random
from django.core.mail import send_mail
from .forms import VehicleDocumentForm, InsuranceDocumentForm
from datetime import datetime, timedelta
from django.utils import timezone
from django.utils.timezone import localtime
from django.http import JsonResponse, HttpResponse
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.cache import never_cache
from django.db.models import Q, Sum, Count, Avg, Max
from django.db import IntegrityError
from .activity_logger import log_activity


def generate_otp():
    return str(random.randint(100000, 999999))


def custom_csrf_failure_view(request, reason=""):
    """
    Catches any CSRF failures across mobile devices, in-app QR scanners, and Safari webviews.
    Instead of crashing with an ugly yellow 403 Forbidden screen, gracefully redirects back safely.
    """
    path = request.path or ''
    if '/mess/' in path:
        return redirect(f"{reverse('mess_login_view')}?csrf_retry=1")
    elif '/camp/' in path:
        return redirect(f"{reverse('camp_root_view')}?csrf_retry=1")

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', ''):
        return JsonResponse({'status': 'CSRF_REFRESH', 'msg': 'Session refreshed. Please retry.'}, status=403)

    referer = request.META.get('HTTP_REFERER')
    if referer and referer != request.build_absolute_uri():
        return redirect(referer)
    return redirect('auth')


def _resolve_worker_and_user(identifier):
    """
    Robust resolver for worker identity across:
    1. Employee ID (exact, case-insensitive, or numeric suffix e.g. '2843' for 'HR-EMP-26082843')
    2. Phone number (exact, or formatted with country code/dashes/spaces matching clean digits)
    3. User username, email, or phone number
    Returns (emp_obj, user_obj) tuple.
    """
    ident = str(identifier or '').strip()
    if not ident:
        return None, None

    clean_digits = re.sub(r'\D', '', ident)

    # 1. Direct exact employee match (emp_id or contact_info)
    emp = Employee.objects.filter(
        Q(emp_id__iexact=ident) |
        Q(contact_info__iexact=ident)
    ).first()

    # 2. Direct exact user match (username, email, phone)
    user = User.objects.filter(
        Q(username__iexact=ident) |
        Q(email__iexact=ident) |
        Q(phone_number__iexact=ident)
    ).first()

    # 3. If phone digits provided (>= 7 digits for Bhutan 8-digit or India 10-digit)
    if not emp and len(clean_digits) >= 7:
        phone_suffix = clean_digits[-8:]
        emp = Employee.objects.filter(
            Q(contact_info__icontains=clean_digits) |
            Q(contact_info__icontains=phone_suffix)
        ).first()

    if not user and len(clean_digits) >= 7:
        phone_suffix = clean_digits[-8:]
        user = User.objects.filter(
            Q(phone_number__icontains=clean_digits) |
            Q(phone_number__icontains=phone_suffix) |
            Q(username__icontains=phone_suffix)
        ).first()

    # 4. If numeric or alphanumeric suffix entered for emp_id (e.g. '2843' for 'HR-EMP-26082843')
    if not emp and len(ident) >= 3:
        emp = Employee.objects.filter(emp_id__iendswith=ident).first()
        if not emp:
            emp = Employee.objects.filter(emp_id__icontains=ident).first()

    if not user and len(ident) >= 3:
        user = User.objects.filter(username__iendswith=ident).first()

    # 5. Link emp and user if one was found and not the other
    if emp and not user:
        user = User.objects.filter(
            Q(employee=emp) |
            Q(username__iexact=emp.emp_id) |
            Q(phone_number__iexact=emp.contact_info)
        ).first()
        if not user and emp.contact_info:
            emp_digits = re.sub(r'\D', '', emp.contact_info)
            if len(emp_digits) >= 7:
                user = User.objects.filter(phone_number__icontains=emp_digits[-8:]).first()

    if user and not emp:
        if getattr(user, 'employee', None):
            emp = user.employee
        else:
            emp = Employee.objects.filter(
                Q(emp_id__iexact=user.username) |
                Q(contact_info__iexact=user.phone_number)
            ).first()

    return emp, user


@csrf_exempt
@never_cache
@ensure_csrf_cookie
def auth_view(request):
    next_url = request.GET.get('next') or request.POST.get('next') or ''
    if next_url.startswith('/mess/'):
        return redirect('mess_login_view')

    if request.user.is_authenticated:
        return redirect('dashboard')
        
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'login':
            raw_login_id = request.POST.get('email') or request.POST.get('username') or ''
            login_id = raw_login_id.strip()
            raw_password = request.POST.get('password') or ''
            password = raw_password.strip()

            user_obj = None
            if login_id:
                user_obj = User.objects.filter(
                    Q(email__iexact=login_id) |
                    Q(username__iexact=login_id) |
                    Q(phone_number__iexact=login_id)
                ).first()
                if not user_obj:
                    _, user_obj = _resolve_worker_and_user(login_id)

            username_to_auth = user_obj.username if user_obj else login_id
            
            # Try authenticate with stripped password first, fallback to raw password
            user = authenticate(request, username=username_to_auth, password=password)
            if user is None and raw_password != password:
                user = authenticate(request, username=username_to_auth, password=raw_password)

            # Direct check_password fallback
            if user is None and user_obj:
                if user_obj.check_password(password) or user_obj.check_password(raw_password):
                    user = user_obj
                    user.backend = 'django.contrib.auth.backends.ModelBackend'

            if user is not None:
                if not user.is_active:
                    messages.error(request, 'This account is disabled. Please contact System Manager.')
                    return render(request, 'login.html', {'show_otp': False})
                login(request, user)
                request.session.set_expiry(60 * 60 * 24 * 30) # 30 days persistence
                request.session.modified = True
                log_activity(user, 'LOGIN', 'Auth', f"{user.full_name or user.username} logged in.", request)
                next_url = request.GET.get('next')
                if next_url and next_url.startswith('/'):
                    return redirect(next_url)
                return redirect('dashboard')
            else:
                messages.error(request, 'Invalid credentials. Please verify your Email/Username/Phone and Password.')
                
        elif action == 'signup':
            full_name = request.POST.get('full_name')
            phone_number = request.POST.get('phone_number')
            email = request.POST.get('email')
            password = request.POST.get('password')
            
            if User.objects.filter(username=email).exists():
                messages.error(request, 'Email already registered.')
            else:
                otp = generate_otp()
                request.session['signup_data'] = {
                    'full_name': full_name,
                    'phone_number': phone_number,
                    'email': email,
                    'password': password,
                    'otp': otp
                }
                
                subject = 'Your OTP for P&M Account Verification'
                message = f'Dear {full_name},\n\nYour One Time Password (OTP) is: {otp}\n\nRegards,\nManager\nP&M'
                try:
                    send_mail(subject, message, None, [email], fail_silently=False)
                    messages.success(request, 'Details saved! Please enter the OTP sent to your email.')
                    return render(request, 'login.html', {'show_otp': True})
                except Exception as e:
                    messages.error(request, 'Error sending OTP email. Check SMTP configuration.')
                    print("Mail error:", e)

        elif action == 'verify_otp':
            entered_otp = request.POST.get('otp')
            signup_data = request.session.get('signup_data')
            
            if signup_data and entered_otp == signup_data.get('otp'):
                user = User.objects.create_user(
                    username=signup_data['email'], 
                    email=signup_data['email'], 
                    password=signup_data['password'],
                    full_name=signup_data['full_name'],
                    phone_number=signup_data['phone_number'],
                    system_role='PENDING'
                )
                del request.session['signup_data']
                user = authenticate(request, username=signup_data['email'], password=signup_data['password'])
                login(request, user)
                return redirect('dashboard')
            else:
                messages.error(request, 'Invalid OTP. Please try again.')
                return render(request, 'login.html', {'show_otp': True})
                
    return render(request, 'login.html', {'show_otp': False})

def logout_view(request):
    if request.user.is_authenticated:
        log_activity(request.user, 'LOGOUT', 'Auth', f"{request.user.full_name or request.user.username} logged out.", request)
    logout(request)
    next_url = request.GET.get('next') or request.POST.get('next')
    if next_url and next_url.startswith('/'):
        return redirect(next_url)
    referer = request.META.get('HTTP_REFERER', '')
    if '/mess/' in referer:
        return redirect('mess_login_view')
    return redirect('auth_view')


def _build_card_previews(assigned_modules):
    """Compute small data previews shown beneath each dashboard stat card."""
    import datetime
    from django.db.models import Sum
    from portal.models import DailyDeployment, Employee, EmployeeDocument, VehicleDocument, InsuranceDocument
    from fleet.models import LubricationLog, VehicleMovement, TyreLog, FleetVehicle, HiredVehicle, RepairLog

    today = datetime.date.today()
    month_start = today.replace(day=1)
    p = {}

    if 'daily_deployment' in assigned_modules:
        latest_dep = DailyDeployment.objects.order_by('-date', '-id').first()
        p['dep_today'] = DailyDeployment.objects.filter(date=today).count()
        p['dep_latest_date'] = latest_dep.date if latest_dep else None

    if 'vehicle_movement' in assigned_modules:
        qs = VehicleMovement.objects.all()
        p['movements_total'] = qs.count()
        p['movements_today'] = qs.filter(movement_date=today).count()
        last_mv = qs.first()
        p['movements_last'] = f"{last_mv.vehicle_number} → {last_mv.destination or '-'}" if last_mv else None

    if 'tyre_section' in assigned_modules:
        tq = TyreLog.objects.filter(date__gte=month_start)
        p['tyre_month_count'] = tq.count()
        p['tyre_month_cost'] = float(tq.aggregate(s=Sum('total_amount'))['s'] or 0)

    if 'lubricants' in assigned_modules:
        lq = LubricationLog.objects.filter(date__gte=month_start)
        p['lub_month_count'] = lq.count()
        last_lub = LubricationLog.objects.order_by('-date', '-id').first()
        try:
            p['lub_last'] = f"{last_lub.vehicle.regn} · {last_lub.oil_type}" if last_lub else None
        except Exception:
            p['lub_last'] = None

    if 'spare_parts' in assigned_modules:
        from fleet.models import SparePart, SparePartTransaction
        from django.db.models import F
        p['sp_low_stock'] = SparePart.objects.filter(current_stock__lte=F('reorder_level')).count()
        p['sp_month_in'] = SparePartTransaction.objects.filter(date__gte=month_start, transaction_type='IN').aggregate(s=Sum('quantity'))['s'] or 0
        p['sp_month_out'] = SparePartTransaction.objects.filter(date__gte=month_start, transaction_type='OUT').aggregate(s=Sum('quantity'))['s'] or 0

    if 'total_employees' in assigned_modules:
        p['emp_active'] = Employee.objects.filter(status='Active').count()
        p['emp_leave'] = Employee.objects.filter(status='On Leave').count()
        p['emp_term'] = Employee.objects.filter(status='Terminated').count()
        p['foreign_active'] = Employee.objects.exclude(nationality__icontains='bhutan').filter(status='Active').count()
        p['national_active'] = Employee.objects.filter(nationality__icontains='bhutan', status='Active').count()

    if 'total_vehicles' in assigned_modules:
        total_v = FleetVehicle.objects.count()
        workshop_ids = RepairLog.objects.filter(
            out_date__isnull=True, vehicle__isnull=False
        ).values_list('vehicle_id', flat=True).distinct()
        workshop = len(set(workshop_ids))
        p['veh_running'] = max(total_v - workshop, 0)
        p['veh_workshop'] = workshop
        p['veh_hired'] = HiredVehicle.objects.count()

    if 'document_expiries' in assigned_modules:
        soon_30 = today + timedelta(days=30)
        soon_90 = today + timedelta(days=90)
        
        expired = 0
        expired += EmployeeDocument.objects.filter(expiry_date__lte=today).count()
        expired += VehicleDocument.objects.filter(rc_expiry_date__lte=today).count()
        expired += InsuranceDocument.objects.filter(expiry_date__lte=today).count()
        
        critical = 0
        critical += EmployeeDocument.objects.filter(expiry_date__gt=today, expiry_date__lte=soon_30).count()
        critical += VehicleDocument.objects.filter(rc_expiry_date__gt=today, rc_expiry_date__lte=soon_30).count()
        critical += InsuranceDocument.objects.filter(expiry_date__gt=today, expiry_date__lte=soon_30).count()
        
        warning = 0
        warning += EmployeeDocument.objects.filter(expiry_date__gt=soon_30, expiry_date__lte=soon_90).count()
        warning += VehicleDocument.objects.filter(rc_expiry_date__gt=soon_30, rc_expiry_date__lte=soon_90).count()
        warning += InsuranceDocument.objects.filter(expiry_date__gt=soon_30, expiry_date__lte=soon_90).count()
        
        p['docs_expired'] = expired
        p['docs_critical'] = critical
        p['docs_warning'] = warning
        p['docs_expiring_soon'] = critical

    if 'attendance_management' in assigned_modules:
        try:
            from portal.models import EmployeeAttendance
            today_att = EmployeeAttendance.objects.filter(date=today)
            p['att_present'] = today_att.filter(status='Present').count()
            p['att_absent'] = today_att.filter(status='Absent').count()
            p['att_total'] = today_att.count()
        except Exception:
            pass

    if 'breakdown_register' in assigned_modules:
        try:
            active_bd_cnt = RepairLog.objects.filter(out_date__isnull=True).count()
            completed_bd_cnt = RepairLog.objects.filter(out_date__isnull=False).count()
            today_bd_cnt = RepairLog.objects.filter(in_date=today).count()
            p['bd_active'] = active_bd_cnt
            p['bd_completed'] = completed_bd_cnt
            p['bd_today'] = today_bd_cnt
        except Exception:
            pass

    return p


@login_required
def dashboard_view(request):
    import datetime, calendar
    from django.db.models import Sum
    from fleet.models import LubricationLog
    from portal.models import DailyDeployment, Employee, EmployeeDocument, VehicleDocument, InsuranceDocument, MessLog, CampMovementLog

    user = request.user
    
    if user.system_role == 'PENDING' and not user.is_superuser:
        return render(request, 'dashboard.html', {'role': user.system_role, 'pending': True})

    # Generate or update document expiries alerts — Manager/Admin only
    is_manager = user.system_role == 'MANAGER' or user.is_superuser
    if is_manager:
        today = timezone.now().date()
        settings = SystemSettings.objects.first()
        days_pref = settings.doc_expiry_threshold if settings else 90
        
        current_alert_titles = []
        
        def get_milestone_info(doc_type_label, name_or_reg, expiry_date):
            if not expiry_date:
                return None, None
            days_left = (expiry_date - today).days
            if days_left > days_pref:
                return None, None
                
            if days_left <= 0:
                days_str = "EXPIRED"
                msg = f"{doc_type_label} expired on {expiry_date} (Overdue by {-days_left} days)."
            elif days_left <= 15:
                days_str = "Expiring in 15 Days"
                msg = f"{doc_type_label} expires on {expiry_date} ({days_left} days remaining)."
            elif days_left <= 30:
                days_str = "Expiring in 30 Days"
                msg = f"{doc_type_label} expires on {expiry_date} ({days_left} days remaining)."
            elif days_left <= 60:
                days_str = "Expiring in 60 Days"
                msg = f"{doc_type_label} expires on {expiry_date} ({days_left} days remaining)."
            elif days_left <= 90:
                days_str = "Expiring in 90 Days"
                msg = f"{doc_type_label} expires on {expiry_date} ({days_left} days remaining)."
            else:
                days_str = f"Expiring in {days_pref} Days"
                msg = f"{doc_type_label} expires on {expiry_date} ({days_left} days remaining)."
                
            title = f"{doc_type_label} {days_str}: {name_or_reg}"
            return title, msg

        if user.notification_prefs.get('docs', True):
            # Vehicle Documents
            for doc in VehicleDocument.objects.all():
                title, msg = get_milestone_info("RC", doc.registration_no, doc.rc_expiry_date)
                if title:
                    current_alert_titles.append(title)
                    if not Notification.objects.filter(user=user, title=title).exists():
                        Notification.objects.create(user=user, title=title, message=msg, notification_type='ALERT', link=f"/documents/alerts/?q={doc.registration_no}&tab=vehicles")
            
            # Insurance Documents
            for doc in InsuranceDocument.objects.all():
                title, msg = get_milestone_info("Insurance", doc.registration_no, doc.expiry_date)
                if title:
                    current_alert_titles.append(title)
                    if not Notification.objects.filter(user=user, title=title).exists():
                        Notification.objects.create(user=user, title=title, message=msg, notification_type='ALERT', link=f"/documents/alerts/?q={doc.registration_no}&tab=insurance")
                        
            # Employee Work Permits
            for emp in Employee.objects.all():
                title, msg = get_milestone_info("Permit", emp.name, emp.work_permit_expiry)
                if title:
                    current_alert_titles.append(title)
                    if not Notification.objects.filter(user=user, title=title).exists():
                        Notification.objects.create(user=user, title=title, message=msg, notification_type='ALERT', link=f"/documents/alerts/?q={emp.name}&tab=employees")

            # Employee Documents
            for doc in EmployeeDocument.objects.select_related('employee').all():
                name = doc.employee.name if doc.employee else 'Unknown'
                title, msg = get_milestone_info("Doc", name, doc.expiry_date)
                if title:
                    current_alert_titles.append(title)
                    if not Notification.objects.filter(user=user, title=title).exists():
                        Notification.objects.create(user=user, title=title, message=msg, notification_type='ALERT', link=f"/documents/alerts/?q={name}&tab=employees")

            Notification.objects.filter(user=user, notification_type='ALERT').exclude(title__in=current_alert_titles).delete()
    
    if is_manager:
        settings = SystemSettings.objects.first()
        retention_days = settings.trash_retention_days if settings else 30
        
        sections = []

                # Dynamic module vehicles (if any)
        dynamic_vehicles = Vehicle.objects.all()
        employees = User.objects.all().order_by('full_name')
        
        # Department & Dynamic Modules Analytics
        total_dynamic_modules = 0
        total_data_entries = []
        
        # Live Activity Feed (Last 15 entries)
        recent_activities = []
        
        # Module Utilization
        from django.db.models import Count
        module_utilization = []
        
        # Cross-module Analytics for Fleet
        all_fleet = FleetVehicle.objects.all()
        total_fleet_count = all_fleet.count()
        
        ongoing_repairs = RepairLog.objects.filter(out_date__isnull=True)
        bd_count = ongoing_repairs.count()
        
        avail_pct = round(((total_fleet_count - bd_count) / total_fleet_count * 100), 1) if total_fleet_count > 0 else 0
        
        overdue_services = ServiceLog.objects.filter(status='overdue').count()
        
        recent_alerts = list(ongoing_repairs.order_by('-in_date')[:5])
        
        # Department stats
        dept_stats = []
        for sec in sections:
            if hasattr(sec, 'fleet_vehicles'):
                s_total = 0
                s_bd = 0
                dept_stats.append({
                    "name": sec.name,
                    "total": s_total,
                    "bd": s_bd
                })
                
        # Master Fleet Data
        master_fleet = []
        for v in all_fleet:
            v_repairs = RepairLog.objects.filter(vehicle=v, out_date__isnull=True)
            status = "In Workshop" if v_repairs.exists() else "Running"
            
            day_driver = "Unassigned"
            night_driver = "Unassigned"
            ownership = "In-House"
            if isinstance(v.extra_data, dict):
                day_driver = v.extra_data.get('day_driver', 'Unassigned')
                night_driver = v.extra_data.get('night_driver', 'Unassigned')
                ownership = v.extra_data.get('ownership', 'In-House')
            
            # Find latest activity date
            last_repair = RepairLog.objects.filter(vehicle=v).order_by('-in_date').first()
            last_activity_date = last_repair.in_date if last_repair else v.hmr_date
                
            master_fleet.append({
                "id": v.id,
                "dno": v.dno,
                "regn": v.regn,
                "section_id": "",
                "section_name": "Unknown",
                "status": status,
                "ownership": ownership,
                "day_driver": day_driver,
                "night_driver": night_driver,
                "latest_hmr": v.latest_hmr,
                "last_activity_date": last_activity_date
            })

        # Employee Data (from Employee model, not website Users)
        actual_employees = Employee.objects.all()
        emp_data = []
        
        import datetime
        
        expiring_employees = []
        today = timezone.now().date()
        thirty_days = today + timedelta(days=30)
        
        for emp in actual_employees:
            # Check for expiring documents
            exp_status = "Valid"
            if emp.work_permit_expiry:
                if emp.work_permit_expiry < today:
                    exp_status = "Expired"
                elif emp.work_permit_expiry <= thirty_days:
                    exp_status = "Expiring Soon"
                    
            if exp_status != "Valid":
                expiring_employees.append({
                    "name": emp.name,
                    "emp_id": emp.emp_id,
                    "permit": emp.work_permit_no,
                    "expiry": emp.work_permit_expiry,
                    "status": exp_status
                })

        for vdoc in VehicleDocument.objects.all():
            if vdoc.rc_expiry_date and vdoc.rc_expiry_date <= thirty_days:
                expiring_employees.append({
                    "name": vdoc.driver_operator or vdoc.registration_no,
                    "emp_id": vdoc.registration_no,
                    "permit": "Vehicle RC",
                    "expiry": vdoc.rc_expiry_date,
                    "status": "Expired" if vdoc.rc_expiry_date < today else "Expiring Soon"
                })
        for idoc in InsuranceDocument.objects.all():
            if idoc.expiry_date and idoc.expiry_date <= thirty_days:
                expiring_employees.append({
                    "name": idoc.driver_operator or idoc.registration_no,
                    "emp_id": idoc.policy_no or idoc.registration_no,
                    "permit": "Insurance Policy",
                    "expiry": idoc.expiry_date,
                    "status": "Expired" if idoc.expiry_date < today else "Expiring Soon"
                })

        
        national_count = Employee.objects.filter(nationality__icontains='bhutan').count()
        foreign_count = Employee.objects.exclude(nationality__icontains='bhutan').count()
        
        shift_day = actual_employees.filter(current_shift='Day').count()
        shift_night = actual_employees.filter(current_shift='Night').count()
        # --- LUBRICANTS AND DOCUMENT EXPIRED CALCULATION ---

        today_date = datetime.date.today()
        
        # 1. Daily Deployments
        deployments_count = DailyDeployment.objects.filter(date=today_date).count()
        
        # 2. Lubricants Cost for the latest active month containing data
        month_start = today_date.replace(day=1)
        month_logs = LubricationLog.objects.filter(date__gte=month_start)
        if not month_logs.exists():
            latest_log = LubricationLog.objects.order_by('-date').first()
            if latest_log:
                month_start = latest_log.date.replace(day=1)
                _, last_day = calendar.monthrange(month_start.year, month_start.month)
                month_logs = LubricationLog.objects.filter(date__range=(month_start, month_start.replace(day=last_day)))
        lubricants_cost = month_logs.aggregate(total=Sum('total_amount'))['total'] or 0
        
        # 3. Expired Documents (expiry_date <= today)
        expired_emp = EmployeeDocument.objects.filter(expiry_date__lte=today_date).count()
        expired_vdoc = VehicleDocument.objects.filter(rc_expiry_date__lte=today_date).count()
        expired_ins = InsuranceDocument.objects.filter(expiry_date__lte=today_date).count()
        expired_docs_count = expired_emp + expired_vdoc + expired_ins
        hired_fleet_count = FleetVehicle.objects.filter(extra_data__ownership__iexact='Hired').count()
        in_house_fleet_count = total_fleet_count - hired_fleet_count
        running_fleet_count = max(0, total_fleet_count - bd_count)

        stats = {
            'employees': actual_employees.count(),
            'national': national_count,
            'foreign': foreign_count,
            'vehicles': total_fleet_count,
            'sections': 0,
            'total_fleet': total_fleet_count,
            'hired_fleet': hired_fleet_count,
            'in_house_fleet': in_house_fleet_count,
            'running_fleet': running_fleet_count,
            'active_bd': bd_count,
            'avail_pct': avail_pct,
            'overdue_srv': overdue_services,
            'shift_day': shift_day,
            'shift_night': shift_night,
            'deployments': deployments_count,
            'lubricants_cost': round(float(lubricants_cost), 2),
            'expired_docs': expired_docs_count,
            'camp_inside_count': actual_employees.filter(camp_status='INSIDE').count(),
            'mess_today_meals': MessLog.objects.filter(date=timezone.localdate(), status='SUCCESS').count()
        }
        
        # Pass new variables to context via context dict at the end

        import datetime
        from collections import Counter
        
        # 1. Top 5 Vehicles and Repeat Offenders
        top_5_vehicles = RepairLog.objects.values('vehicle__dno', 'vehicle__regn').annotate(
            repair_count=Count('id')
        ).order_by('-repair_count')[:5]
        
        # Repeat offenders: Vehicles that had more than 1 repair in the last 30 days
        thirty_days_ago = timezone.now().date() - timedelta(days=30)
        recent_repairs = RepairLog.objects.filter(in_date__gte=thirty_days_ago)
        repeat_offenders = recent_repairs.values('vehicle__dno').annotate(
            recent_count=Count('id')
        ).filter(recent_count__gt=1).order_by('-recent_count')[:5]

        # 2. Most Used Replacement Parts
        all_parts = RepairLog.objects.exclude(parts_used__isnull=True).exclude(parts_used__exact='').values_list('parts_used', flat=True)
        parts_counter = Counter()
        for part_str in all_parts:
            # simple comma split and clean
            parts = [p.strip().lower() for p in part_str.replace('+', ',').replace('&', ',').split(',') if p.strip()]
            parts_counter.update(parts)
        top_parts = [{'part': p.title(), 'count': c} for p, c in parts_counter.most_common(10)]

        # 3. Breakdown Categorization
        all_complaints = RepairLog.objects.exclude(complaint__isnull=True).exclude(complaint__exact='').values_list('complaint', flat=True)
        category_counts = {
            'Engine & Hydraulic': 0,
            'Electrical': 0,
            'Tyres & Wheels': 0,
            'Brakes': 0,
            'Structural & Body': 0,
            'Other': 0
        }
        for comp in all_complaints:
            comp_lower = comp.lower()
            if any(k in comp_lower for k in ['engine', 'oil', 'leak', 'hydraulic', 'pump', 'cylinder']):
                category_counts['Engine & Hydraulic'] += 1
            elif any(k in comp_lower for k in ['wire', 'light', 'battery', 'electrical', 'sensor', 'switch', 'starter', 'alternator']):
                category_counts['Electrical'] += 1
            elif any(k in comp_lower for k in ['tyre', 'tire', 'puncture', 'wheel', 'alignment']):
                category_counts['Tyres & Wheels'] += 1
            elif any(k in comp_lower for k in ['brake', 'pad', 'shoe', 'air leak', 'abs']):
                category_counts['Brakes'] += 1
            elif any(k in comp_lower for k in ['glass', 'body', 'door', 'cabin', 'seat', 'mirror', 'chassis', 'welding']):
                category_counts['Structural & Body'] += 1
            else:
                category_counts['Other'] += 1
                
        breakdown_categories = [{'category': k, 'count': v} for k, v in category_counts.items() if v > 0]

        # 4. Monthly Fleet Downtime & Repair Frequency
        # We need to get total repair count per month, and total downtime days per month
        # Since SQLite doesn't easily do date diffs in ORM, we do it in Python
        all_logs = RepairLog.objects.all()
        monthly_downtime = {}
        for log in all_logs:
            if not log.in_date: continue
            month_key = log.in_date.strftime('%Y-%b')
            sort_key = log.in_date.strftime('%Y-%m')
            
            out_d = log.out_date if log.out_date else datetime.date.today()
            downtime_days = (out_d - log.in_date).days
            if downtime_days < 0: downtime_days = 0
            
            if month_key not in monthly_downtime:
                monthly_downtime[month_key] = {'month': month_key, 'sort': sort_key, 'repair_count': 0, 'downtime_days': 0}
            
            monthly_downtime[month_key]['repair_count'] += 1
            monthly_downtime[month_key]['downtime_days'] += downtime_days
            
        monthly_downtime_list = sorted(monthly_downtime.values(), key=lambda x: x['sort'])[-6:] # last 6 months

        extra_context = {
            'stats': stats,
            'dept_stats': dept_stats,
            'recent_alerts': recent_alerts,
            'recent_entries': [],
            'master_fleet': master_fleet[:100],
            'emp_data': actual_employees[:100],
            'expiring_employees': expiring_employees,
            'top_5_vehicles': list(top_5_vehicles),
            'repeat_offenders': list(repeat_offenders),
            'top_parts': top_parts,
            'breakdown_categories': json.dumps(breakdown_categories),
            'monthly_downtime': json.dumps(monthly_downtime_list),
            'total_fleet_count': total_fleet_count,
            'total_dynamic_modules': 0,
            'total_data_entries': 0,
            'bd_count': bd_count,
        }
    else:
        assigned = getattr(user, 'assigned_modules', []) or []
        
        # --- LUBRICANTS AND DOCUMENT EXPIRED CALCULATION FOR EMPLOYEES ---

        today_date = datetime.date.today()
        deployments_count = DailyDeployment.objects.filter(date=today_date).count()
        
        month_start = today_date.replace(day=1)
        month_logs = LubricationLog.objects.filter(date__gte=month_start)
        if not month_logs.exists():
            latest_log = LubricationLog.objects.order_by('-date').first()
            if latest_log:  
                month_start = latest_log.date.replace(day=1)
                _, last_day = calendar.monthrange(month_start.year, month_start.month)
                month_logs = LubricationLog.objects.filter(date__range=(month_start, month_start.replace(day=last_day)))
        lubricants_cost = month_logs.aggregate(total=Sum('total_amount'))['total'] or 0
        
        expired_emp = EmployeeDocument.objects.filter(expiry_date__lte=today_date).count()
        expired_vdoc = VehicleDocument.objects.filter(rc_expiry_date__lte=today_date).count()
        expired_ins = InsuranceDocument.objects.filter(expiry_date__lte=today_date).count()
        expired_docs_count = expired_emp + expired_vdoc + expired_ins

        stats = {
            'deployments': deployments_count,
            'lubricants_cost': round(float(lubricants_cost), 2),
            'expired_docs': expired_docs_count,
            'employees': 0,
            'national': 0,
            'foreign': 0,
            'vehicles': 0,
            'shift_day': 0,
            'shift_night': 0
        }
        if 'total_employees' in assigned:
            stats['employees'] = Employee.objects.count()
            stats['national'] = Employee.objects.filter(nationality__icontains='bhutan').count()
            stats['foreign'] = Employee.objects.exclude(nationality__icontains='bhutan').count()
        if 'total_vehicles' in assigned:
            stats['vehicles'] = FleetVehicle.objects.count()
        if 'shift_management' in assigned:
            stats['shift_day'] = Employee.objects.filter(current_shift='Day').count()
            stats['shift_night'] = Employee.objects.filter(current_shift='Night').count()
        if 'vehicle_movement' in assigned:
            from fleet.models import VehicleMovement
            stats['vehicle_movements'] = VehicleMovement.objects.count()
        if 'tyre_section' in assigned:
            from fleet.models import TyreLog
            stats['tyre_count'] = TyreLog.objects.count()
        if 'overtime_management' in assigned or user.system_role in ['TIME_KEEPER', 'PROJECT_MANAGER']:
            today_ot = OvertimeRecord.objects.filter(date=timezone.localdate())
            stats['ot_today_hours'] = round(sum(r.overtime_hours for r in today_ot), 1)
            stats['ot_today_workers'] = today_ot.values('emp_id_snapshot').distinct().count()
            stats['ot_pending_count'] = today_ot.filter(status='Pending').count()

        # --- DEO / TIME_KEEPER CONTEXT ---
        pass

    archived = []
    # For DEOs / Time Keepers: pass their assigned_modules list so template can show only allowed cards/nav
    assigned_modules = list(user.assigned_modules or [])
    if user.is_superuser:
        assigned_modules = [
            'daily_deployment', 'shift_management', 'vehicle_movement', 'tyre_section', 'total_employees',
            'foreign_workers', 'national_workers', 'total_vehicles',
            'lubricants', 'document_expiries', 'spare_parts', 'attendance_management', 'overtime_management', 'manage_logins',
            'breakdown_register', 'vehicle_profiles'
        ]
    elif is_manager:
        base_mgr = [
            'daily_deployment', 'shift_management', 'vehicle_movement', 'tyre_section', 'total_employees',
            'foreign_workers', 'national_workers', 'total_vehicles',
            'lubricants', 'document_expiries', 'spare_parts', 'attendance_management',
            'breakdown_register', 'vehicle_profiles'
        ]
        user_assigned = list(user.assigned_modules or [])
        if 'overtime_management' in user_assigned:
            base_mgr.append('overtime_management')
        if 'manage_logins' in user_assigned:
            base_mgr.append('manage_logins')
        if 'attendance_management' in user_assigned and 'attendance_management' not in base_mgr:
            base_mgr.append('attendance_management')
        assigned_modules = base_mgr
    elif user.system_role == 'TIME_KEEPER':
        assigned_modules = list(user.assigned_modules or [])
        if 'overtime_management' not in assigned_modules:
            assigned_modules.append('overtime_management')
        if 'attendance_management' not in assigned_modules:
            assigned_modules.append('attendance_management')
    else:
        assigned_modules = list(user.assigned_modules or [])
    latest_dep = DailyDeployment.objects.order_by('-id').first()
    latest_emp = Employee.objects.order_by('-id').first()
    from fleet.models import LubricationLog
    latest_lub = LubricationLog.objects.order_by('-id').first()
    context = {
        'sections': [],
        'archived_sections': archived,
        'stats': stats,
        'is_manager': is_manager,
        'role': user.system_role,
        'pending': False,
        'assigned_modules': assigned_modules,
        'latest_dep_id': latest_dep.id if latest_dep else 0,
        'latest_emp_id': latest_emp.id if latest_emp else 0,
        'latest_lub_id': latest_lub.id if latest_lub else 0,
        'total_deps': DailyDeployment.objects.count(),
        'total_emps': Employee.objects.count(),
        'total_lubs': LubricationLog.objects.count(),
        'card_previews': _build_card_previews(assigned_modules),
    }
    if is_manager:
        context.update(extra_context)

    has_social_feed_perm = request.user.is_superuser or request.user.is_staff or getattr(request.user, 'enable_social_feed_mode', False)
    view_mode_param = request.GET.get('view_mode')

    if not has_social_feed_perm:
        active_view_mode = 'cards'
        request.session['dashboard_view_mode'] = 'cards'
        post_param = request.GET.get('post') or request.GET.get('view_post') or request.GET.get('ref')
        if post_param:
            return redirect('post_router_view', entry_key=post_param)
    else:
        if view_mode_param in ['social', 'cards']:
            request.session['dashboard_view_mode'] = view_mode_param
        active_view_mode = request.session.get('dashboard_view_mode', 'cards')
        if view_mode_param == 'social':
            active_view_mode = 'social'

    if has_social_feed_perm and active_view_mode == 'social':
        context['feed_posts'] = _build_all_modules_social_feed(request.user)
        context['has_social_feed_perm'] = True
        context['current_view_mode'] = 'social'
        return render(request, 'dashboard_social_feed.html', context)

    context['has_social_feed_perm'] = has_social_feed_perm
    context['current_view_mode'] = 'cards'
    return render(request, 'dashboard.html', context)


def _format_post_time(dt, has_exact_time=True):
    if not dt:
        return ''
    from django.utils import timezone
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    loc = timezone.localtime(dt)
    now = timezone.localtime(timezone.now())
    time_part = loc.strftime('%I:%M %p')
    if not has_exact_time:
        if loc.date() == now.date():
            return "Today"
        yesterday = (now - timezone.timedelta(days=1)).date()
        if loc.date() == yesterday:
            return "Yesterday"
        return loc.strftime('%b %d, %Y')

    if loc.date() == now.date():
        return f"Today, {time_part}"
    yesterday = (now - timezone.timedelta(days=1)).date()
    if loc.date() == yesterday:
        return f"Yesterday, {time_part}"
    return loc.strftime('%b %d, %I:%M %p')

def _build_all_modules_social_feed(user):
    from portal.models import CampMovementLog, MessLog, DailyDeployment, Employee, FeedEntryLike, FeedEntryComment, OvertimeRecord, User
    from fleet.models import LubricationLog, RepairLog
    from django.db.models import Count
    from django.utils import timezone

    feed_posts = []
    first_other_user = User.objects.exclude(id=user.id).exclude(system_role='PENDING').filter(is_active=True).order_by('full_name', 'username').first()

    def get_chat_uid(author_uid):
        if author_uid and str(author_uid) != str(user.id):
            return author_uid
        if first_other_user:
            return first_other_user.id
        return ""
    
    # 1. Fetch user likes set, counts and like summaries
    user_likes_set = set(FeedEntryLike.objects.filter(user=user).values_list('entry_key', flat=True))
    likes_dict = dict(FeedEntryLike.objects.values('entry_key').annotate(cnt=Count('id')).values_list('entry_key', 'cnt'))
    comments_dict = dict(FeedEntryComment.objects.values('entry_key').annotate(cnt=Count('id')).values_list('entry_key', 'cnt'))
    
    # Pre-build liked by summary dictionary
    all_likes = FeedEntryLike.objects.select_related('user').order_by('created_at')
    likes_by_key = {}
    for lk in all_likes:
        k = lk.entry_key
        if k not in likes_by_key:
            likes_by_key[k] = []
        u_name = lk.user.full_name or lk.user.username
        likes_by_key[k].append(u_name)

    liked_summary_dict = {}
    for k, names in likes_by_key.items():
        cnt = len(names)
        if cnt == 1:
            liked_summary_dict[k] = f"Liked by {names[0]}"
        elif cnt == 2:
            liked_summary_dict[k] = f"Liked by {names[0]} and {names[1]}"
        elif cnt > 2:
            liked_summary_dict[k] = f"Liked by {names[0]}, {names[1]} and {cnt - 2} others"

    all_comments = FeedEntryComment.objects.select_related('user').order_by('created_at')
    comments_by_key = {}
    for c in all_comments:
        k = c.entry_key
        if k not in comments_by_key:
            comments_by_key[k] = []
        time_str = timezone.localtime(c.created_at).strftime('%b %d, %I:%M %p')
        comments_by_key[k].append({
            'id': c.id,
            'user_name': c.user.full_name or c.user.username,
            'comment_text': c.comment_text,
            'time': time_str
        })

    # A. Camp Movement Logs (Last 30)
    for l in CampMovementLog.objects.select_related('employee', 'scanned_by').order_by('-timestamp', '-id')[:30]:
        key = f"camp_{l.id}"
        dt = l.timestamp
        has_time = True
        if not dt:
            dt = timezone.make_aware(timezone.datetime.combine(l.date, timezone.datetime.min.time())) if l.date else timezone.now()
            has_time = False
        time_str = _format_post_time(dt, has_exact_time=has_time)
        emp_code = f"EMP-{l.employee.id:04d}" if '@' in l.employee.emp_id else l.employee.emp_id
        
        direction_text = "Returned to Camp" if l.direction == 'IN' else "Exited Camp"
        details_text = f"Purpose: {l.get_purpose_display()} via {l.gate_name or 'Main Gate'}"
        if l.remarks:
            details_text += f" — \"{l.remarks}\""
        
        officer_name = (l.scanned_by.full_name or l.scanned_by.username) if l.scanned_by else "Camp Gate Guard"
        officer_role = "Camp Security Officer"
        author_user_id = l.scanned_by.id if l.scanned_by else ""
            
        feed_posts.append({
            'key': key,
            'timestamp': dt,
            'icon': '🏠' if l.direction == 'IN' else '🚪',
            'module_label': 'Camp Movement',
            'badge_class': 'badge-camp',
            'avatar_bg': 'linear-gradient(135deg, #0284c7, #38bdf8)',
            'author_name': l.employee.name,
            'author_sub': f"#{emp_code} • {l.employee.department or 'Site'}",
            'submitter_name': officer_name,
            'submitter_role': officer_role,
            'submitter_title': f"{officer_name} ({officer_role})",
            'author_user_id': author_user_id,
            'chat_user_id': get_chat_uid(author_user_id),
            'title': f"{direction_text} ({l.direction})",
            'details': details_text,
            'time_str': time_str,
            'is_liked': key in user_likes_set,
            'like_count': likes_dict.get(key, 0),
            'liked_summary': liked_summary_dict.get(key, ''),
            'comment_count': comments_dict.get(key, 0),
            'comments': comments_by_key.get(key, [])
        })

    # B. Mess Scans (Last 30)
    for m in MessLog.objects.select_related('employee', 'mess_location', 'scanned_by').order_by('-punch_time', '-id')[:30]:
        key = f"mess_{m.id}"
        m_time = getattr(m, 'punch_time', None)
        has_time = True
        if m_time:
            dt = m_time
        elif m.date:
            dt = timezone.make_aware(timezone.datetime.combine(m.date, timezone.datetime.min.time()))
            has_time = False
        else:
            dt = timezone.now()
            has_time = False
        time_str = _format_post_time(dt, has_exact_time=has_time)
        emp_name = m.employee.name if m.employee else "Guest Worker"
        emp_code = m.employee.emp_id if m.employee else "N/A"
        loc_name = m.mess_location.name if getattr(m, 'mess_location', None) else "Mess Desk"
        
        status_text = "Meal Token Verified" if m.status == 'SUCCESS' else f"Scan Failed ({m.remarks or 'Denied'})"
        officer_name = (m.scanned_by.full_name or m.scanned_by.username) if m.scanned_by else "Mess Supervisor"
        officer_role = "Mess Supervisor"
        author_user_id = m.scanned_by.id if m.scanned_by else ""

        feed_posts.append({
            'key': key,
            'timestamp': dt,
            'icon': '🍱',
            'module_label': 'Mess Management',
            'badge_class': 'badge-mess',
            'avatar_bg': 'linear-gradient(135deg, #f59e0b, #d97706)',
            'author_name': emp_name,
            'author_sub': f"#{emp_code} • {m.meal_type or 'Meal'}",
            'submitter_name': officer_name,
            'submitter_role': officer_role,
            'submitter_title': f"{officer_name} ({officer_role})",
            'author_user_id': author_user_id,
            'chat_user_id': get_chat_uid(author_user_id),
            'title': status_text,
            'details': f"Meal: {m.meal_type or 'Token'} • Location: {loc_name}",
            'time_str': time_str,
            'is_liked': key in user_likes_set,
            'like_count': likes_dict.get(key, 0),
            'liked_summary': liked_summary_dict.get(key, ''),
            'comment_count': comments_dict.get(key, 0),
            'comments': comments_by_key.get(key, [])
        })

    # C. Daily Deployments (Last 25)
    default_deo = User.objects.filter(system_role='DEO').order_by('id').first()
    for d in DailyDeployment.objects.select_related('entered_by').order_by('-created_at', '-id')[:25]:
        key = f"dep_{d.id}"
        d_time = getattr(d, 'created_at', None)
        has_time = True
        if d_time:
            dt = d_time
        elif d.date:
            dt = timezone.make_aware(timezone.datetime.combine(d.date, timezone.datetime.min.time()))
            has_time = False
        else:
            dt = timezone.now()
            has_time = False
        time_str = _format_post_time(dt, has_exact_time=has_time)
        total_workers = d.total_day + d.total_night
        if getattr(d, 'entered_by', None):
            officer_name = d.entered_by.full_name or d.entered_by.username
            author_user_id = d.entered_by.id
        elif default_deo:
            officer_name = default_deo.full_name or default_deo.username
            author_user_id = default_deo.id
        else:
            officer_name = "Abhishek Anand"
            author_user_id = ""
        officer_role = "Deployment Officer"
        
        feed_posts.append({
            'key': key,
            'timestamp': dt,
            'icon': '📋',
            'module_label': 'Daily Deployment',
            'badge_class': 'badge-ot',
            'avatar_bg': 'linear-gradient(135deg, #ec4899, #8b5cf6)',
            'author_name': officer_name,
            'author_sub': f"Deployment Officer • Machinery: {d.machinery}",
            'submitter_name': officer_name,
            'submitter_role': officer_role,
            'submitter_title': f"{officer_name} ({officer_role})",
            'author_user_id': author_user_id,
            'chat_user_id': get_chat_uid(author_user_id),
            'title': f"Daily Deployment Logged ({d.machinery})",
            'details': f"Day: {d.total_day} Workers • Night: {d.total_night} Workers • Total: {total_workers}",
            'time_str': time_str,
            'is_liked': key in user_likes_set,
            'like_count': likes_dict.get(key, 0),
            'liked_summary': liked_summary_dict.get(key, ''),
            'comment_count': comments_dict.get(key, 0),
            'comments': comments_by_key.get(key, [])
        })

    # D. Fleet Vehicle Repairs (Last 25)
    for r in RepairLog.objects.select_related('vehicle', 'logged_by').order_by('-created_at', '-in_date', '-id')[:25]:
        key = f"rep_{r.id}"
        r_time = getattr(r, 'created_at', None)
        has_time = True
        if r_time:
            dt = r_time
        elif getattr(r, 'in_time', None) and r.in_date:
            dt = timezone.make_aware(timezone.datetime.combine(r.in_date, r.in_time))
        elif r.in_date:
            dt = timezone.make_aware(timezone.datetime.combine(r.in_date, timezone.datetime.min.time()))
            has_time = False
        else:
            dt = timezone.now()
            has_time = False
        time_str = _format_post_time(dt, has_exact_time=has_time)
        regn = r.vehicle.regn if r.vehicle else "Fleet Item"
        dno = r.vehicle.dno if r.vehicle else ""
        
        officer_name = (r.logged_by.full_name or r.logged_by.username) if getattr(r, 'logged_by', None) else (r.mechanic or "Fleet Incharge")
        officer_role = "Fleet / Workshop Incharge"
        author_user_id = r.logged_by.id if getattr(r, 'logged_by', None) else ""
        
        feed_posts.append({
            'key': key,
            'timestamp': dt,
            'icon': '🚜',
            'module_label': 'Fleet Maintenance',
            'badge_class': 'badge-vehicle',
            'avatar_bg': 'linear-gradient(135deg, #10b981, #059669)',
            'author_name': f"Vehicle {dno} ({regn})",
            'author_sub': f"Maintenance Log #{r.id}",
            'submitter_name': officer_name,
            'submitter_role': officer_role,
            'submitter_title': f"{officer_name} ({officer_role})",
            'author_user_id': author_user_id,
            'chat_user_id': get_chat_uid(author_user_id),
            'title': f"Repair Entry: {r.complaint or 'Scheduled Service'}",
            'details': f"Status: {'In Workshop' if not r.out_date else 'Repaired & Out'} • Parts: {r.parts_used or 'N/A'}",
            'time_str': time_str,
            'is_liked': key in user_likes_set,
            'like_count': likes_dict.get(key, 0),
            'liked_summary': liked_summary_dict.get(key, ''),
            'comment_count': comments_dict.get(key, 0),
            'comments': comments_by_key.get(key, [])
        })

    # E. Overtime Records (Last 25)
    for ot in OvertimeRecord.objects.select_related('time_keeper').order_by('-created_at', '-date', '-id')[:25]:
        key = f"ot_{ot.id}"
        ot_time = getattr(ot, 'created_at', None)
        has_time = True
        if ot_time:
            dt = ot_time
        elif ot.date:
            dt = timezone.make_aware(timezone.datetime.combine(ot.date, timezone.datetime.min.time()))
            has_time = False
        else:
            dt = timezone.now()
            has_time = False
        time_str = _format_post_time(dt, has_exact_time=has_time)
        
        officer_name = (ot.time_keeper.full_name or ot.time_keeper.username) if getattr(ot, 'time_keeper', None) else (ot.time_keeper_name or "Site Time Keeper")
        officer_role = "Site Time Keeper"
        author_user_id = ot.time_keeper.id if getattr(ot, 'time_keeper', None) else ""
        
        feed_posts.append({
            'key': key,
            'timestamp': dt,
            'icon': '⏱️',
            'module_label': 'Overtime Entry',
            'badge_class': 'badge-ot',
            'avatar_bg': 'linear-gradient(135deg, #7c3aed, #a855f7)',
            'author_name': ot.employee_name,
            'author_sub': f"#{ot.emp_id_snapshot} • {ot.department or 'Site'}",
            'submitter_name': officer_name,
            'submitter_role': officer_role,
            'submitter_title': f"{officer_name} ({officer_role})",
            'author_user_id': author_user_id,
            'chat_user_id': get_chat_uid(author_user_id),
            'title': f"Recorded {ot.overtime_hours} Hours Overtime ({ot.shift} Shift)",
            'details': f"Zone: {ot.location_zone} • Task: {ot.work_description or 'Regular Overtime'} • Status: {ot.status}",
            'time_str': time_str,
            'is_liked': key in user_likes_set,
            'like_count': likes_dict.get(key, 0),
            'liked_summary': liked_summary_dict.get(key, ''),
            'comment_count': comments_dict.get(key, 0),
            'comments': comments_by_key.get(key, [])
        })

    # Strictly sort by newest timestamp first
    feed_posts.sort(key=lambda x: x['timestamp'] if x.get('timestamp') else timezone.now(), reverse=True)
    return feed_posts[:50]


def _get_single_post_normal_details(clean_key):
    try:
        from portal.models import CampMovementLog, MessLog, DailyDeployment, OvertimeRecord, FeedEntryComment
        from fleet.models import RepairLog
        k = str(clean_key).strip().replace('post-card-', '')
        
        comments_qs = FeedEntryComment.objects.filter(entry_key=k).select_related('user').order_by('created_at')
        from django.utils import timezone
        comments = []
        for c in comments_qs:
            comments.append({
                'user_name': c.user.full_name or c.user.username,
                'comment_text': c.comment_text,
                'time': timezone.localtime(c.created_at).strftime('%b %d, %I:%M %p')
            })

        if k.startswith('camp_'):
            lid = int(k.split('_')[1])
            obj = CampMovementLog.objects.select_related('employee', 'scanned_by').filter(id=lid).first()
            if not obj:
                return None
            officer = (obj.scanned_by.full_name or obj.scanned_by.username) if obj.scanned_by else "Camp Gate Guard"
            return {
                'key': k,
                'module_name': 'Camp Gate Movement',
                'icon': '🏠' if obj.direction == 'IN' else '🚪',
                'badge_color': '#0284c7',
                'record_id': f"CAMP-LOG #{obj.id:04d}",
                'title': f"{'Returned to Camp' if obj.direction == 'IN' else 'Exited Camp'} ({obj.direction})",
                'date_time': _format_post_time(obj.timestamp, has_exact_time=True),
                'submitter_name': officer,
                'submitter_role': "Camp Security Officer",
                'submitter_user_id': obj.scanned_by.id if obj.scanned_by else None,
                'module_url': '/camp/dashboard/',
                'module_url_label': 'Go to Camp Management Hub',
                'comments': comments,
                'fields': [
                    ('Employee Name', obj.employee.name if obj.employee else 'N/A'),
                    ('Employee ID', obj.employee.emp_id if obj.employee else 'N/A'),
                    ('Department / Site', obj.employee.department or 'Site') if obj.employee else ('Department', 'Site'),
                    ('Gate Name', obj.gate_name or 'Main Gate'),
                    ('Movement Direction', f"{'🟢 Entry / In' if obj.direction == 'IN' else '🔴 Exit / Out'}"),
                    ('Purpose', obj.get_purpose_display()),
                    ('Entry Mode', obj.entry_mode or 'GUARD_SCAN'),
                    ('Remarks / Reason', obj.remarks or 'None'),
                ]
            }
        elif k.startswith('mess_'):
            lid = int(k.split('_')[1])
            obj = MessLog.objects.select_related('employee', 'mess_location', 'scanned_by').filter(id=lid).first()
            if not obj:
                return None
            officer = (obj.scanned_by.full_name or obj.scanned_by.username) if obj.scanned_by else "Mess Supervisor"
            return {
                'key': k,
                'module_name': 'Mess Management',
                'icon': '🍱',
                'badge_color': '#d97706',
                'record_id': f"MESS-LOG #{obj.id:04d}",
                'title': f"Meal Punch: {obj.get_meal_type_display()} ({obj.get_status_display()})",
                'date_time': _format_post_time(obj.punch_time, has_exact_time=True),
                'submitter_name': officer,
                'submitter_role': "Mess Supervisor",
                'submitter_user_id': obj.scanned_by.id if obj.scanned_by else None,
                'module_url': '/mess/dashboard/',
                'module_url_label': 'Go to Mess Management Hub',
                'comments': comments,
                'fields': [
                    ('Employee / Worker', obj.employee.name if obj.employee else 'Guest Worker'),
                    ('Employee ID', obj.employee.emp_id if obj.employee else 'N/A'),
                    ('Meal Type', obj.get_meal_type_display()),
                    ('Verification Status', obj.get_status_display()),
                    ('Mess Location', obj.mess_location.name if obj.mess_location else 'Mess Desk'),
                    ('Punch Mode', obj.entry_mode or 'SCANNER'),
                    ('Remarks', obj.remarks or 'Verified successfully'),
                ]
            }
        elif k.startswith('dep_'):
            lid = int(k.split('_')[1])
            obj = DailyDeployment.objects.select_related('entered_by').filter(id=lid).first()
            if not obj:
                return None
            default_deo = User.objects.filter(system_role='DEO').order_by('id').first()
            if getattr(obj, 'entered_by', None):
                officer = obj.entered_by.full_name or obj.entered_by.username
                sub_uid = obj.entered_by.id
            elif default_deo:
                officer = default_deo.full_name or default_deo.username
                sub_uid = default_deo.id
            else:
                officer = "Abhishek Anand"
                sub_uid = None
            return {
                'key': k,
                'module_name': 'Daily Deployment',
                'icon': '📋',
                'badge_color': '#db2777',
                'record_id': f"DEP-LOG #{obj.id:04d}",
                'title': f"Daily Deployment: {obj.machinery}",
                'date_time': _format_post_time(obj.created_at, has_exact_time=True),
                'submitter_name': officer,
                'submitter_role': "Deployment Officer",
                'submitter_user_id': sub_uid,
                'module_url': '/deployment/',
                'module_url_label': 'Go to Daily Deployment Hub',
                'comments': comments,
                'fields': [
                    ('Date', obj.date.strftime('%d %B %Y') if obj.date else 'N/A'),
                    ('Machinery / Equipment', obj.machinery),
                    ('Day Shift Workers', obj.total_day),
                    ('Night Shift Workers', obj.total_night),
                    ('Total Deployment', obj.total_day + obj.total_night),
                    ('Zone 1 & 2', f"Day: {obj.zone_1_2_day} | Night: {obj.zone_1_2_night}"),
                    ('Zone 3 & 4', f"Day: {obj.zone_3_4_day} | Night: {obj.zone_3_4_night}"),
                    ('Borrow Area', f"Day: {obj.borrow_area_day} | Night: {obj.borrow_area_night}"),
                    ('Culvert Area', f"Day: {obj.culvert_area_day} | Night: {obj.culvert_area_night}"),
                    ('Batching Plant', f"Day: {obj.batching_plant_day} | Night: {obj.batching_plant_night}"),
                    ('Crushing Plant', f"Day: {obj.crushing_plant_day} | Night: {obj.crushing_plant_night}"),
                    ('Road Maintenance', f"Day: {obj.road_maint_day} | Night: {obj.road_maint_night}"),
                ]
            }
        elif k.startswith('rep_'):
            lid = int(k.split('_')[1])
            obj = RepairLog.objects.select_related('vehicle', 'logged_by').filter(id=lid).first()
            if not obj:
                return None
            officer = (obj.logged_by.full_name or obj.logged_by.username) if obj.logged_by else (obj.mechanic or "Fleet Incharge")
            regn = obj.vehicle.regn if obj.vehicle else "Fleet Item"
            dno = obj.vehicle.dno if obj.vehicle else ""
            return {
                'key': k,
                'module_name': 'Fleet Maintenance / Repairs',
                'icon': '🚜',
                'badge_color': '#059669',
                'record_id': f"REP-LOG #{obj.id:04d}",
                'title': f"Repair Entry: Vehicle {dno} ({regn})",
                'date_time': _format_post_time(obj.created_at, has_exact_time=True),
                'submitter_name': officer,
                'submitter_role': "Fleet / Workshop Incharge",
                'submitter_user_id': obj.logged_by.id if obj.logged_by else None,
                'module_url': '/fleet/',
                'module_url_label': 'Go to Fleet Management Hub',
                'comments': comments,
                'fields': [
                    ('Vehicle Regn / DNO', f"{dno} ({regn})"),
                    ('Vehicle Type', obj.vehicle.vehicle_type if obj.vehicle else 'Equipment'),
                    ('Workshop Status', 'COMPLETED (Repaired & Out)' if obj.out_date else 'IN WORKSHOP (Under Repair)'),
                    ('In Date / Time', f"{obj.in_date} {obj.in_time or ''}"),
                    ('Out Date / Time', f"{obj.out_date or 'Still in workshop'} {obj.out_time or ''}"),
                    ('Complaint / Issue', obj.complaint or 'Scheduled Service'),
                    ('Parts Used', obj.parts_used or 'N/A'),
                    ('Quantity', obj.qty or 'N/A'),
                    ('Assigned Mechanic', obj.mechanic or 'Workshop Staff'),
                    ('Remarks', obj.remarks or 'None'),
                ]
            }
        elif k.startswith('ot_'):
            lid = int(k.split('_')[1])
            obj = OvertimeRecord.objects.select_related('time_keeper').filter(id=lid).first()
            if not obj:
                return None
            officer = (obj.time_keeper.full_name or obj.time_keeper.username) if obj.time_keeper else (obj.time_keeper_name or "Site Time Keeper")
            return {
                'key': k,
                'module_name': 'Overtime Records',
                'icon': '⏱️',
                'badge_color': '#7c3aed',
                'record_id': f"OT-LOG #{obj.id:04d}",
                'title': f"Overtime: {obj.employee_name} ({obj.overtime_hours} hrs)",
                'date_time': _format_post_time(obj.created_at, has_exact_time=True),
                'submitter_name': officer,
                'submitter_role': "Site Time Keeper",
                'submitter_user_id': obj.time_keeper.id if obj.time_keeper else None,
                'module_url': '/overtime/',
                'module_url_label': 'Go to Overtime Hub',
                'comments': comments,
                'fields': [
                    ('Employee Name', obj.employee_name),
                    ('Employee ID', obj.emp_id_snapshot),
                    ('Department / Designation', f"{obj.department} • {obj.designation}"),
                    ('Date', obj.date.strftime('%d %B %Y') if obj.date else 'N/A'),
                    ('Shift', obj.shift),
                    ('Punch Type', obj.get_punch_type_display()),
                    ('Timings', f"{obj.in_time or ''} - {obj.out_time or ''}"),
                    ('Overtime Hours', f"{obj.overtime_hours} Hours"),
                    ('Location / Zone', obj.location_zone),
                    ('Task / Activity Assigned', obj.work_description or 'Regular Overtime'),
                    ('Approval Status', obj.status),
                    ('Remarks', obj.remarks or 'None'),
                ]
            }
    except Exception:
        pass
    return None


@login_required
def post_router_view(request, entry_key):
    """
    Routes a post view request:
    - If user has social feed permission: redirects to social feed card with smooth scroll & pulse glow.
    - If user does NOT have social feed permission: renders the post in clean, professional Normal View (post_normal_view.html).
    """
    from django.contrib import messages
    has_social_feed_perm = request.user.is_superuser or request.user.is_staff or getattr(request.user, 'enable_social_feed_mode', False)
    clean_key = str(entry_key).strip().replace('post-card-', '')

    if has_social_feed_perm:
        return redirect(f"/dashboard/?view_mode=social#post-card-{clean_key}")

    post_data = _get_single_post_normal_details(clean_key)
    if not post_data:
        messages.error(request, f"Post with reference #{clean_key} could not be found.")
        return redirect('dashboard')

    return render(request, 'post_normal_view.html', {
        'post': post_data,
        'entry_key': clean_key,
    })


def user_has_manage_logins_permission(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.system_role in ['MANAGER', 'PROJECT_MANAGER']:
        return True
    assigned = getattr(user, 'assigned_modules', None) or []
    if 'manage_logins' in assigned:
        return True
    return False


def _classify_mess_worker(u):
    mods = u.assigned_modules or []
    if 'office_staff' in mods:
        return False
    if 'mess_worker_only' in mods:
        return True
    if u.is_superuser or u.is_staff or u.system_role in ['MANAGER', 'PROJECT_MANAGER', 'DEO', 'TIME_KEEPER', 'ADMIN', 'STAFF', 'EMPLOYEE']:
        return False
    if any(m in mods for m in ['daily_deployment', 'vehicle_movement', 'breakdown_register', 'breakdown_entry', 'vehicle_profiles', 'camp_manager', 'spare_parts', 'tyre_section', 'lubricants', 'fuel', 'manage_logins', 'overtime_management', 'attendance_register', 'camp', 'camp_guard', 'camp_hr']):
        return False
    if u.post is not None:
        return False
    
    emp = u.employee
    dept = (emp.department or '').lower() if emp else ''
    desig = (emp.designation or '').lower() if emp else ''

    # If explicitly assigned to Mess department or Cook/Kitchen trade
    if any(k in dept for k in ['mess', 'canteen', 'kitchen']) or any(k in desig for k in ['cook', 'chef', 'mess boy', 'canteen', 'kitchen']):
        return True

    # If department belongs to company site/office workforce (tunnel, plant, 108 chorten, admin, hr, etc.)
    if dept and not any(k in dept for k in ['mess', 'canteen', 'kitchen']):
        return False

    return False


@login_required
def teams_view(request):
    user = request.user
    if not user_has_manage_logins_permission(user):
        messages.error(request, "Permission denied: You do not have permission to manage logins.")
        return redirect('dashboard')
        
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_user':
            full_name = request.POST.get('full_name', '').strip()
            username = request.POST.get('username', '').strip()
            email = request.POST.get('email', '').strip()
            phone_number = request.POST.get('phone_number', '').strip()
            password = request.POST.get('password', '').strip()
            system_role = request.POST.get('system_role', 'DEO')
            post_id = request.POST.get('post')
            modules = request.POST.getlist('modules')

            if not username:
                username = email

            if User.objects.filter(Q(username__iexact=username) | (Q(email__iexact=email) if email else Q(pk=None))).exists():
                messages.error(request, f"User with username '{username}' or email '{email}' already exists.")
            elif not password:
                messages.error(request, "Password is required to create a new account.")
            else:
                new_user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    full_name=full_name,
                    phone_number=phone_number,
                    system_role=system_role,
                    assigned_modules=modules
                )
                if post_id:
                    try:
                        new_user.post = CompanyPost.objects.get(id=post_id)
                        new_user.save()
                    except Exception:
                        pass
                log_activity(user, 'CREATE', 'User Management', f"Created new login user '{new_user.username}' ({new_user.get_system_role_display()}).", request)
                messages.success(request, f"New system user account '{new_user.username}' created successfully!")
                return redirect(f"{reverse('teams')}?tab=staff")

        elif action == 'create_mess_worker':
            worker_name = request.POST.get('worker_name', '').strip()
            worker_emp_id = request.POST.get('worker_emp_id', '').strip()
            worker_phone = request.POST.get('worker_phone', '').strip()
            worker_permit = request.POST.get('worker_permit', '').strip()
            worker_pin = request.POST.get('worker_pin', '').strip()
            assigned_mess_id = request.POST.get('assigned_mess_id')
            department = request.POST.get('department', 'Site Operations').strip()
            designation = request.POST.get('designation', 'Worker / Diner').strip()

            if not worker_name or not worker_emp_id or not worker_pin:
                messages.error(request, "Name, Employee ID, and 4-Digit PIN are required.")
            elif User.objects.filter(username__iexact=worker_emp_id).exists():
                messages.error(request, f"A login account with Employee ID '{worker_emp_id}' already exists.")
            else:
                mess_loc = MessLocation.objects.filter(id=assigned_mess_id).first() if assigned_mess_id else MessLocation.objects.filter(is_active=True).first()
                emp_obj = Employee.objects.filter(
                    Q(emp_id__iexact=worker_emp_id) | (Q(contact_info__iexact=worker_phone) if worker_phone else Q(pk=None))
                ).first()

                if not emp_obj:
                    emp_obj = Employee.objects.create(
                        emp_id=worker_emp_id,
                        name=worker_name,
                        contact_info=worker_phone,
                        work_permit_no=worker_permit,
                        department=department,
                        designation=designation,
                        assigned_mess=mess_loc
                    )
                else:
                    if worker_permit and not emp_obj.work_permit_no:
                        emp_obj.work_permit_no = worker_permit
                    if mess_loc and not emp_obj.assigned_mess:
                        emp_obj.assigned_mess = mess_loc
                    emp_obj.save()

                new_worker_user = User.objects.create_user(
                    username=worker_emp_id,
                    password=worker_pin,
                    full_name=worker_name,
                    phone_number=worker_phone,
                    system_role='USER',
                    assigned_modules=['mess_user'],
                    employee=emp_obj
                )
                log_activity(user, 'CREATE', 'Mess Worker Management', f"Created Mess Diner account for {worker_name} ({worker_emp_id}).", request)
                messages.success(request, f"Mess Diner account created for '{worker_name}' ({worker_emp_id})!")
                return redirect(f"{reverse('teams')}?tab=mess")

    all_users = User.objects.filter(is_superuser=False).select_related('employee', 'post', 'employee__assigned_mess').order_by('-date_joined')
    posts = CompanyPost.objects.all()
    mess_locations = MessLocation.objects.filter(is_active=True).order_by('name')

    today = timezone.localtime(timezone.now()).date()

    # Pre-fetch today's logs for quick meal status summary
    today_mess_logs = MessLog.objects.filter(date=today, status='SUCCESS').select_related('mess_location').values(
        'employee_id', 'meal_type', 'verification_token', 'punch_time', 'mess_location__name'
    )
    logs_by_emp = {}
    for log in today_mess_logs:
        emp_id = log['employee_id']
        if emp_id not in logs_by_emp:
            logs_by_emp[emp_id] = {}
        logs_by_emp[emp_id][log['meal_type']] = log

    system_staff = []
    mess_workers = []

    for u in all_users:
        is_mess = _classify_mess_worker(u)
        if is_mess:
            emp = u.employee or _get_or_create_user_employee(u)
            emp_meals = logs_by_emp.get(emp.id, {}) if emp else {}
            u.cached_emp = emp
            u.breakfast_log = emp_meals.get('BREAKFAST')
            u.lunch_log = emp_meals.get('LUNCH')
            u.dinner_log = emp_meals.get('DINNER')
            u.has_eaten_today = bool(emp_meals)
            mess_workers.append(u)
        else:
            system_staff.append(u)

    active_tab = request.GET.get('tab', 'staff')
    if active_tab not in ['staff', 'mess']:
        active_tab = 'staff'

    return render(request, 'teams.html', {
        'system_staff': system_staff,
        'mess_workers': mess_workers,
        'employees': all_users,
        'posts': posts,
        'mess_locations': mess_locations,
        'active_tab': active_tab,
        'today_date': today,
    })


@login_required
def api_switch_user_category(request):
    """
    1-Click API to transfer any user between 'System & Office Staff' and 'Mess Workers & Diners'.
    """
    if not user_has_manage_logins_permission(request.user):
        return JsonResponse({'status': 'ERROR', 'msg': 'Permission denied.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body) if request.body else request.POST
        user_id = data.get('user_id')
        target = data.get('target', 'staff') # 'staff' or 'mess'
        user_obj = get_object_or_404(User, id=user_id)

        mods = list(user_obj.assigned_modules or [])
        if target == 'staff':
            if 'mess_worker_only' in mods:
                mods.remove('mess_worker_only')
            if 'office_staff' not in mods:
                mods.append('office_staff')
            if user_obj.system_role in ['USER', 'PENDING']:
                user_obj.system_role = data.get('role', 'DEO')
            user_obj.assigned_modules = mods
            user_obj.save()
            log_activity(request.user, 'UPDATE', 'User Management', f"Moved {user_obj.username} to Office Staff.", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"'{user_obj.full_name or user_obj.username}' has been successfully moved to System & Office Staff!"})
        else:
            if 'office_staff' in mods:
                mods.remove('office_staff')
            if 'mess_worker_only' not in mods:
                mods.append('mess_worker_only')
            user_obj.system_role = 'USER'
            if 'mess_user' not in mods:
                mods.append('mess_user')
            user_obj.assigned_modules = mods
            user_obj.save()
            log_activity(request.user, 'UPDATE', 'User Management', f"Moved {user_obj.username} to Mess Workers.", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"'{user_obj.full_name or user_obj.username}' has been successfully moved to Mess Workers section!"})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def api_save_mess_worker_details(request):
    """
    API to edit all worker details from Manage Logins (Name, Emp ID, Phone, Permit, Mess, Post, Shift, Status, PIN, Category).
    """
    if not user_has_manage_logins_permission(request.user):
        return JsonResponse({'status': 'ERROR', 'msg': 'Permission denied.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body) if request.body else request.POST
        user_id = data.get('user_id')
        user_obj = get_object_or_404(User, id=user_id)
        emp_obj = user_obj.employee or _get_or_create_user_employee(user_obj)

        name = str(data.get('name', '')).strip()
        emp_id = str(data.get('emp_id', '')).strip()
        phone_number = str(data.get('phone_number', '')).strip()
        work_permit_no = str(data.get('work_permit_no', '')).strip()
        assigned_mess_id = data.get('assigned_mess_id')
        department = str(data.get('department', '')).strip()
        designation = str(data.get('designation', '')).strip()
        current_shift = str(data.get('current_shift', '')).strip()
        is_active = data.get('is_active')
        if is_active is not None:
            is_active = bool(is_active) if not isinstance(is_active, str) else (is_active.lower() in ['1', 'true', 'yes'])
        else:
            is_active = user_obj.is_active

        new_pin = str(data.get('new_pin', '')).strip()
        category = str(data.get('account_category', '')).strip()
        system_role = str(data.get('system_role', '')).strip()

        # Update Category & System Role
        mods = list(user_obj.assigned_modules or [])
        if category == 'staff':
            if 'mess_worker_only' in mods:
                mods.remove('mess_worker_only')
            if 'office_staff' not in mods:
                mods.append('office_staff')
            if system_role:
                user_obj.system_role = system_role
            elif user_obj.system_role in ['USER', 'PENDING']:
                user_obj.system_role = 'DEO'
        elif category == 'mess':
            if 'office_staff' in mods:
                mods.remove('office_staff')
            if 'mess_worker_only' not in mods:
                mods.append('mess_worker_only')
            user_obj.system_role = 'USER'
            if 'mess_user' not in mods:
                mods.append('mess_user')

        user_obj.assigned_modules = mods

        # Update User
        if name:
            user_obj.full_name = name
        if phone_number:
            user_obj.phone_number = phone_number
        user_obj.is_active = is_active

        if emp_id and emp_id != user_obj.username:
            if User.objects.filter(username__iexact=emp_id).exclude(id=user_obj.id).exists():
                return JsonResponse({'status': 'ERROR', 'msg': f"Username / Employee ID '{emp_id}' is already in use by another account."}, status=400)
            user_obj.username = emp_id

        if new_pin:
            if len(new_pin) < 4:
                return JsonResponse({'status': 'ERROR', 'msg': 'The 4-digit PIN must be at least 4 digits.'}, status=400)
            user_obj.set_password(new_pin)

        user_obj.save()

        # Update Employee record synchronously
        if emp_obj:
            if name:
                emp_obj.name = name
            if emp_id:
                emp_obj.emp_id = emp_id
            if phone_number:
                emp_obj.contact_info = phone_number
            emp_obj.work_permit_no = work_permit_no
            if department:
                emp_obj.department = department
            if designation:
                emp_obj.designation = designation
            if current_shift:
                emp_obj.current_shift = current_shift
            emp_obj.is_active = is_active

            if assigned_mess_id:
                emp_obj.assigned_mess = MessLocation.objects.filter(id=assigned_mess_id).first()
            elif assigned_mess_id == '' or assigned_mess_id == 0:
                emp_obj.assigned_mess = None

            emp_obj.save()

        log_activity(request.user, 'UPDATE', 'Worker Management', f"Updated details for {user_obj.full_name or user_obj.username}.", request)
        return JsonResponse({
            'status': 'SUCCESS',
            'msg': f"Details updated successfully for {user_obj.full_name or user_obj.username}!"
        })
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def api_get_mess_worker_meal_status(request, user_id):
    """
    Returns real-time meal approval status for today across all meal windows for a worker.
    """
    if not user_has_manage_logins_permission(request.user):
        return JsonResponse({'status': 'ERROR', 'msg': 'Permission denied.'}, status=403)

    user_obj = get_object_or_404(User, id=user_id)
    emp_obj = user_obj.employee or _get_or_create_user_employee(user_obj)
    today = timezone.localtime(timezone.now()).date()

    logs = MessLog.objects.filter(employee=emp_obj, date=today).select_related('mess_location').order_by('-punch_time')
    
    meals = ['BREAKFAST', 'LUNCH', 'SNACKS', 'DINNER']
    meal_status = {}
    for m in meals:
        log = logs.filter(meal_type=m, status='SUCCESS').first()
        dup_count = logs.filter(meal_type=m, status='DUPLICATE').count()
        if log:
            meal_status[m] = {
                'approved': True,
                'log_id': log.id,
                'token': log.verification_token or f"PASS-{log.id}",
                'time': timezone.localtime(log.punch_time).strftime('%I:%M:%S %p'),
                'mess_name': log.mess_location.name if log.mess_location else "Main Canteen",
                'mess_id': log.mess_location.id if log.mess_location else None,
                'entry_mode': log.entry_mode,
                'remarks': log.remarks or '',
                'duplicates_blocked': dup_count
            }
        else:
            meal_status[m] = {
                'approved': False,
                'log_id': None,
                'token': None,
                'time': None,
                'mess_name': None,
                'mess_id': None,
                'entry_mode': None,
                'remarks': None,
                'duplicates_blocked': dup_count
            }

    messes = list(MessLocation.objects.filter(is_active=True).values('id', 'name'))

    return JsonResponse({
        'status': 'SUCCESS',
        'worker': {
            'user_id': user_obj.id,
            'username': user_obj.username,
            'name': user_obj.full_name or (emp_obj.name if emp_obj else user_obj.username),
            'emp_id': emp_obj.emp_id if emp_obj else user_obj.username,
            'phone': user_obj.phone_number or (emp_obj.contact_info if emp_obj else ''),
            'permit_no': emp_obj.work_permit_no if emp_obj else '',
            'assigned_mess_id': emp_obj.assigned_mess.id if emp_obj and emp_obj.assigned_mess else (messes[0]['id'] if messes else None),
            'assigned_mess_name': emp_obj.assigned_mess.name if emp_obj and emp_obj.assigned_mess else 'Not Assigned',
            'department': emp_obj.department if emp_obj else 'Site Operations',
            'designation': emp_obj.designation if emp_obj else 'Worker / Diner',
            'shift': emp_obj.current_shift if emp_obj else 'Day Shift',
            'is_active': user_obj.is_active,
            'system_role': user_obj.system_role,
            'account_category': 'staff' if ('office_staff' in (user_obj.assigned_modules or [])) or (user_obj.system_role in ['MANAGER', 'PROJECT_MANAGER', 'DEO', 'TIME_KEEPER', 'ADMIN', 'STAFF', 'EMPLOYEE']) else 'mess'
        },
        'today_date': today.strftime('%d %b %Y'),
        'meals': meal_status,
        'mess_locations': messes
    })


@login_required
def api_manage_mess_meal_approval(request):
    """
    API to RESET meal approvals (so worker can re-scan), GRANT manual approvals, or TRANSFER mess locations.
    """
    if not user_has_manage_logins_permission(request.user):
        return JsonResponse({'status': 'ERROR', 'msg': 'Permission denied.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body)
        user_id = data.get('user_id')
        action_type = str(data.get('action', 'RESET')).upper()
        meal_type = str(data.get('meal_type', 'LUNCH')).upper()
        mess_id = data.get('mess_id')

        user_obj = get_object_or_404(User, id=user_id)
        emp_obj = user_obj.employee or _get_or_create_user_employee(user_obj)
        today = timezone.localtime(timezone.now()).date()

        if action_type == 'RESET':
            qs = MessLog.objects.filter(employee=emp_obj, date=today)
            if meal_type != 'ALL':
                qs = qs.filter(meal_type=meal_type)
            count = qs.count()
            qs.delete()

            log_activity(
                request.user, 'RESET', 'Mess Pass Reset',
                f"Reset meal approval for {emp_obj.name} ({meal_type}) on {today}. Worker can now scan again.",
                request
            )
            return JsonResponse({
                'status': 'SUCCESS',
                'msg': f"Approval for {meal_type} has been RESET! {emp_obj.name} can now scan the Mess Wall QR Code again."
            })

        elif action_type == 'GRANT':
            target_mess = MessLocation.objects.filter(id=mess_id).first() if mess_id else (
                emp_obj.assigned_mess or MessLocation.objects.filter(is_active=True).first()
            )
            if not target_mess:
                target_mess = MessLocation.objects.first()

            import uuid
            token = f"PASS-MAN-{uuid.uuid4().hex[:6].upper()}"
            existing = MessLog.objects.filter(employee=emp_obj, date=today, meal_type=meal_type, status='SUCCESS').first()
            if existing:
                return JsonResponse({'status': 'ERROR', 'msg': f"{emp_obj.name} already has an approved pass for {meal_type} ({existing.verification_token})."}, status=400)

            MessLog.objects.create(
                employee=emp_obj,
                mess_location=target_mess,
                date=today,
                meal_type=meal_type,
                scanned_by=request.user,
                status='SUCCESS',
                entry_mode='ADMIN_MANUAL',
                verification_token=token,
                remarks=f"Manually approved by Manager {request.user.username}"
            )
            # Remove any duplicate attempt records
            MessLog.objects.filter(employee=emp_obj, date=today, meal_type=meal_type, status='DUPLICATE').delete()

            log_activity(
                request.user, 'CREATE', 'Mess Pass Override',
                f"Manually approved {meal_type} pass for {emp_obj.name} at {target_mess.name}.",
                request
            )
            return JsonResponse({
                'status': 'SUCCESS',
                'msg': f"Manual approval granted for {meal_type} at {target_mess.name}! Pass Token: {token}"
            })

        elif action_type == 'CHANGE_MESS':
            target_mess = MessLocation.objects.filter(id=mess_id).first()
            if not target_mess:
                return JsonResponse({'status': 'ERROR', 'msg': 'Valid mess location must be selected.'}, status=400)

            log_to_change = MessLog.objects.filter(employee=emp_obj, date=today, meal_type=meal_type, status='SUCCESS').first()
            if not log_to_change:
                return JsonResponse({'status': 'ERROR', 'msg': f"No approved {meal_type} pass found to update."}, status=404)

            old_name = log_to_change.mess_location.name if log_to_change.mess_location else "Main Canteen"
            log_to_change.mess_location = target_mess
            log_to_change.remarks = (log_to_change.remarks or '') + f" [Location changed from {old_name} by {request.user.username}]"
            log_to_change.save()

            log_activity(
                request.user, 'UPDATE', 'Mess Pass Location',
                f"Changed {meal_type} pass location for {emp_obj.name} from {old_name} to {target_mess.name}.",
                request
            )
            return JsonResponse({
                'status': 'SUCCESS',
                'msg': f"Pass location for {meal_type} updated to {target_mess.name} successfully!"
            })

        else:
            return JsonResponse({'status': 'ERROR', 'msg': f"Unknown action '{action_type}'"}, status=400)

    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)

@login_required
def edit_employee_role_view(request, user_id):
    if not user_has_manage_logins_permission(request.user):
        messages.error(request, "Permission denied: You do not have permission to edit user logins.")
        return redirect('dashboard')
        
    employee = get_object_or_404(User, id=user_id)
    all_posts = CompanyPost.objects.all()
    
    if request.method == 'POST':
        full_name = request.POST.get('full_name')
        email = request.POST.get('email')
        phone_number = request.POST.get('phone_number')
        system_role = request.POST.get('system_role')
        post_id = request.POST.get('post')
        new_password = request.POST.get('new_password', '').strip()
        
        if full_name:
            employee.full_name = full_name.strip()
        if email:
            employee.email = email.strip()
        if phone_number:
            employee.phone_number = phone_number.strip()

        employee.system_role = system_role
        if post_id:
            employee.post = get_object_or_404(CompanyPost, id=post_id)
        else:
            employee.post = None
            
        modules = request.POST.getlist('modules')
        employee.assigned_modules = modules
        employee.captain_category = request.POST.get('captain_category', 'All')
        employee.can_view_user_activity = bool(request.POST.get('can_view_user_activity'))

        # Set or Reset Password
        if new_password:
            employee.set_password(new_password)
            
        employee.save()
        log_activity(request.user, 'UPDATE', 'User Management', f"Updated role & permissions for {employee.full_name or employee.username}.", request)
        messages.success(request, f"Roles & permissions updated successfully for {employee.full_name or employee.username}.")
        return redirect('teams')
        
    return render(request, 'edit_employee_role.html', {'employee': employee, 'all_posts': all_posts})

@login_required
def vehicles_view(request):
    user = request.user
    # Temporary simple check
    if user.system_role not in ['MANAGER', 'EMPLOYEE'] and not user.is_superuser:
        return redirect('dashboard')
    
    vehicles = Vehicle.objects.all()
    return render(request, 'vehicles.html', {'vehicles': vehicles, 'is_manager': (user.system_role == 'MANAGER' or user.is_superuser)})





@login_required
def global_search_view(request):
    query = request.GET.get('q', '').strip()
    results = {
        'employees': [],
        'sections': [],
        'entries': []
    }
    
    if query:
        # Search employees
        employees = User.objects.filter(is_superuser=False)
        for emp in employees:
            if (query.lower() in (emp.full_name or '').lower() or 
                query.lower() in (emp.email or '').lower() or 
                query.lower() in (emp.phone_number or '').lower()):
                results['employees'].append(emp)
                
        # Search sections (Modules)
        sections = []

        for sec in sections:
            if query.lower() in sec.name.lower():
                results['sections'].append(sec)
                
            # Check entries in this section
            entries = []
            for entry in entries:
                # search in JSON data
                match_found = False
                for k, v in entry.data.items():
                    if query.lower() in str(v).lower():
                        match_found = True
                        break
                if match_found or query.lower() in (entry.entered_by.full_name or '').lower():
                    results['entries'].append(entry)
                    
    return render(request, 'global_search.html', {'query': query, 'results': results})

@login_required
def employee_profile_view(request, user_id):
    target_user = get_object_or_404(User, id=user_id)
    
    # Only allow managers, superusers, or the user themselves to view the profile
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser and request.user.id != target_user.id:
        return redirect('dashboard')
        
    grouped_data = {}
    return render(request, 'employee_profile.html', {
        'target_user': target_user,
        'grouped_data': grouped_data
    })


@login_required
def send_email_view(request):
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser:
        return redirect('dashboard')
        
    email_type = request.GET.get('type', 'custom')
    employees = User.objects.exclude(system_role='PENDING').exclude(email='')
    
    if request.method == 'POST':
        recipients = request.POST.getlist('recipients')
        subject = request.POST.get('subject')
        message = request.POST.get('message')
        
        if 'all' in recipients:
            recipient_list = [emp.email for emp in employees if emp.email]
        else:
            recipient_list = recipients
            
        if recipient_list and subject and message:
            try:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=recipient_list,
                    fail_silently=False,
                )
                messages.success(request, f"Successfully sent email to {len(recipient_list)} employee(s).")
                return redirect('dashboard')
            except Exception as e:
                messages.error(request, f"Error sending email: {str(e)}")
        else:
            messages.error(request, "Please provide recipients, subject, and message.")
            
    return render(request, 'send_email.html', {
        'email_type': email_type,
        'employees': employees
    })

@login_required
def system_settings_view(request):
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser:
        return redirect('dashboard')
        
    settings_obj = SystemSettings.get_settings()
    
    if request.method == 'POST':
        website_name = request.POST.get('website_name')
        if website_name:
            settings_obj.website_name = website_name
        
        trash_retention_days = request.POST.get('trash_retention_days')
        if trash_retention_days:
            try:
                settings_obj.trash_retention_days = int(trash_retention_days)
            except ValueError:
                pass

        doc_expiry_threshold = request.POST.get('doc_expiry_threshold')
        if doc_expiry_threshold:
            try:
                settings_obj.doc_expiry_threshold = int(doc_expiry_threshold)
            except ValueError:
                pass

        default_working_shift_hours = request.POST.get('default_working_shift_hours')
        if default_working_shift_hours:
            try:
                settings_obj.default_working_shift_hours = int(default_working_shift_hours)
            except ValueError:
                pass

        settings_obj.enable_low_stock_alerts = request.POST.get('enable_low_stock_alerts') == '1'
        settings_obj.enable_email_notifications = request.POST.get('enable_email_notifications') == '1'
        
        if request.POST.get('delete_logo') == '1':
            settings_obj.logo.delete(save=False)
            settings_obj.logo = None
        elif 'logo' in request.FILES:
            settings_obj.logo = request.FILES['logo']
            
        settings_obj.save()
        messages.success(request, 'System settings updated successfully.')
        return redirect('system_settings')
        
    # Rich Context for Central Data Export Hub
    from fleet.models import FleetVehicle, HiredVehicle, SparePart, LubricationLog, TyreLog, SparePartTransaction, VehicleMovement
    from .models import Employee, CompanyPost, DailyVehicleAllocation
    
    vehicles = FleetVehicle.objects.all().order_by('regn')
    parts = SparePart.objects.all().order_by('part_name')
    employees = Employee.objects.filter(status='Active').order_by('name')
    
    # Departments from CompanyPost + Employee department strings
    dept_set = set(CompanyPost.objects.exclude(name='').values_list('name', flat=True))
    dept_set.update(Employee.objects.exclude(department='').values_list('department', flat=True))
    dept_set.update(FleetVehicle.objects.exclude(department__isnull=True).values_list('department__name', flat=True))
    departments = [{'name': d} for d in sorted([d for d in dept_set if d])]
    
    # Extract unique filter values for all dropdowns
    vendors_set = set(FleetVehicle.objects.exclude(extra_data__vendor='').values_list('extra_data__vendor', flat=True))
    vendors_set.update(HiredVehicle.objects.exclude(owner_name='').values_list('owner_name', flat=True))
    vendors_set.update(LubricationLog.objects.exclude(vendor='').values_list('vendor', flat=True))
    vendors_set.update(TyreLog.objects.exclude(vendor='').values_list('vendor', flat=True))
    vendors = sorted([v for v in vendors_set if v])
    
    oil_types = sorted([o for o in LubricationLog.objects.exclude(oil_type='').values_list('oil_type', flat=True).distinct() if o])
    
    locations_set = set(DailyVehicleAllocation.objects.exclude(location_zone='').values_list('location_zone', flat=True))
    locations_set.update(LubricationLog.objects.exclude(location='').values_list('location', flat=True))
    locations_set.update(TyreLog.objects.exclude(location='').values_list('location', flat=True))
    locations_set.update(['Zone 1 & 2', 'Crushing Plant', 'Batching Plant', 'Road Maintenance', 'Gelephu', 'Workshop'])
    locations = sorted([l for l in locations_set if l])
    
    wo_set = set(HiredVehicle.objects.exclude(agreement_ref='').values_list('agreement_ref', flat=True))
    wo_set.update(LubricationLog.objects.exclude(work_order_no='').values_list('work_order_no', flat=True))
    wo_set.update(TyreLog.objects.exclude(work_order_no='').values_list('work_order_no', flat=True))
    work_orders = sorted([w for w in wo_set if w])
    
    context = {
        'settings': settings_obj,
        'vehicles': vehicles,
        'parts': parts,
        'employees': employees,
        'departments': departments,
        'vendors': vendors,
        'oil_types': oil_types,
        'locations': locations,
        'work_orders': work_orders,
        'machinery_categories': ['Scania', 'Excavator', 'Grader', 'Compactor', 'Other'],
    }
    return render(request, 'system_settings.html', context)

@login_required
def my_profile_view(request):
    user = request.user
    
    if request.method == 'POST':
        full_name = request.POST.get('full_name')
        phone_number = request.POST.get('phone_number')
        email = request.POST.get('email')
        
        if full_name:
            user.full_name = full_name
        if phone_number:
            user.phone_number = phone_number
        if email:
            user.email = email
            
        if 'profile_picture' in request.FILES:
            user.profile_picture = request.FILES['profile_picture']
            
        password = request.POST.get('password')
        if password:
            user.set_password(password)
            
        user.save()
        messages.success(request, 'Profile updated successfully. (If you changed your password, you may need to log in again next time).')
        return redirect('my_profile')
        
    return render(request, 'my_profile.html', {'user': user})


@login_required
def update_stock_view(request, entry_id):
    if request.method == 'POST':
        entry = get_object_or_404(id=entry_id)
        
        # Check permissions (must have edit access)
        if request.user.system_role != 'MANAGER' and not request.user.is_superuser:
            if False:
                messages.error(request, 'You do not have permission to update stock.')
                return redirect('view_section_data', section_id=1)
                
        action = request.POST.get('action') # 'ADD' or 'REMOVE'
        qty = int(request.POST.get('quantity', 0))
        remarks = request.POST.get('remarks', '')
        
        if qty <= 0:
            messages.error(request, 'Quantity must be greater than zero.')
            next_url = request.POST.get('next')
            return redirect(next_url) if next_url else redirect('view_section_data', section_id=1)
            
        data = entry.data
        current_stock = int(data.get('current_stock', 0))
        
        from datetime import datetime
        history = data.get('stock_history', [])
        
        if action == 'REMOVE':
            if current_stock < qty:
                messages.error(request, 'Not enough stock available to remove.')
                next_url = request.POST.get('next')
                return redirect(next_url) if next_url else redirect('view_section_data', section_id=1)
            current_stock -= qty
            log_msg = f"REMOVED {qty}"
        else:
            current_stock += qty
            log_msg = f"ADDED {qty}"
            
        history.insert(0, {
            'action': log_msg,
            'remarks': remarks,
            'by': request.user.full_name or request.user.username,
            'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        
        data['current_stock'] = current_stock
        data['stock_history'] = history
        
        entry.data = data
        entry.save()
        messages.success(request, f'Stock updated successfully. Current Stock: {current_stock}')
        
        next_url = request.POST.get('next')
        if next_url:
            return redirect(next_url)
            
        return redirect('view_section_data', section_id=1)
        
    return redirect('dashboard')







@login_required
def search_suggestions_api(request):
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return JsonResponse({'suggestions': []})

    suggestions = []
    
    # 0. Search Static Pages
    static_pages = [
        {'name': 'Dashboard', 'url': '/dashboard/'},
        {'name': 'My Profile', 'url': '/my_profile/'}
    ]
    if request.user.system_role == 'MANAGER' or request.user.is_superuser:
        static_pages.append({'name': 'Manage Logins', 'url': '/teams/'})
        
    for page in static_pages:
        if query.lower() in page['name'].lower():
            suggestions.append({'type': 'System Page', 'text': page['name'], 'url': page['url']})
            
    # 2. Search Employees
    employees = User.objects.filter(is_superuser=False)
    for emp in employees:
        if query.lower() in (emp.full_name or '').lower():
            role = emp.post.name if emp.post else emp.get_system_role_display()
            suggestions.append({'type': 'Employee', 'text': f"{emp.full_name} ({role})", 'url': '/dashboard/'})

    # 3. Search Data Entries
    is_manager = request.user.system_role == 'MANAGER' or request.user.is_superuser

    entries = [].select_related('section').order_by('-created_at')

    count = 0
    for entry in entries:
        if count >= 10:
            break

        # Check permissions
        sec = None
        if not is_manager:
            if not sec.view_users.filter(id=request.user.id).exists() and not sec.edit_users.filter(id=request.user.id).exists():
                continue

        # Search inside data JSON
        match_found = False
        match_text = ""
        if isinstance(entry.data, dict):
            for key, val in entry.data.items():
                if query.lower() in str(val).lower():
                    match_found = True
                    match_text = str(val)
                    break

        if match_found:
            suggestions.append({
                'type': f'{sec.name} Data', 
                'text': match_text[:50] + ('...' if len(match_text) > 50 else ''), 
                'url': f'/section/{sec.id}/data/'
            })
            count += 1

    return JsonResponse({'suggestions': suggestions[:8]})




@login_required
def change_password_view(request):
    if request.method == 'POST':
        old_password = request.POST.get('old_password')
        new_password = request.POST.get('new_password')
        confirm_password = request.POST.get('confirm_password')
        
        if not request.user.check_password(old_password):
            messages.error(request, 'Current password is incorrect.')
            return redirect('change_password')
        
        if new_password != confirm_password:
            messages.error(request, 'New passwords do not match.')
            return redirect('change_password')
        
        if len(new_password) < 6:
            messages.error(request, 'Password must be at least 6 characters.')
            return redirect('change_password')
        
        request.user.set_password(new_password)
        request.user.save()
        messages.success(request, 'Password changed successfully! Please log in again.')
        return redirect('auth_view')
    
    return render(request, 'change_password.html')


def _filter_employees_queryset(qs, params):
    from django.db.models import Q
    import datetime
    today = datetime.date.today()
    
    worker_type = params.get('worker_type') or params.get('nationality')
    status = params.get('status')
    department = params.get('department')
    designation = params.get('designation')
    shift = params.get('shift')
    agency = params.get('agency')
    blood_group = params.get('blood_group')
    expiry = params.get('expiry')
    q = params.get('q') or params.get('search')
    
    if worker_type and worker_type != 'all':
        if worker_type.lower() == 'national':
            qs = qs.filter(nationality__icontains='bhutan')
        elif worker_type.lower() == 'foreign':
            qs = qs.exclude(nationality__icontains='bhutan')
        else:
            qs = qs.filter(nationality__icontains=worker_type)
            
    if status and status != 'all':
        qs = qs.filter(status__iexact=status)
        
    if department and department != 'all':
        qs = qs.filter(department__iexact=department)
        
    if designation and designation != 'all':
        qs = qs.filter(designation__icontains=designation)
        
    if shift and shift != 'all':
        qs = qs.filter(current_shift__icontains=shift)
        
    if agency and agency != 'all':
        qs = qs.filter(contractor_agency__icontains=agency)
        
    if blood_group and blood_group != 'all':
        qs = qs.filter(blood_group__iexact=blood_group)
        
    if expiry and expiry != 'all':
        if expiry == 'expired':
            qs = qs.filter(work_permit_expiry__lt=today)
        elif expiry in ['30_days', '30']:
            qs = qs.filter(work_permit_expiry__gte=today, work_permit_expiry__lte=today + timedelta(days=30))
        elif expiry in ['60_days', '60']:
            qs = qs.filter(work_permit_expiry__gte=today, work_permit_expiry__lte=today + timedelta(days=60))
        elif expiry in ['90_days', '90']:
            qs = qs.filter(work_permit_expiry__gte=today, work_permit_expiry__lte=today + timedelta(days=90))
        elif expiry == 'valid':
            qs = qs.filter(work_permit_expiry__gte=today)
            
    if q:
        qs = qs.filter(
            Q(name__icontains=q) |
            Q(emp_id__icontains=q) |
            Q(cid_number__icontains=q) |
            Q(passport_details__icontains=q) |
            Q(work_permit_no__icontains=q) |
            Q(contractor_agency__icontains=q) |
            Q(designation__icontains=q) |
            Q(department__icontains=q) |
            Q(contact_info__icontains=q) |
            Q(blood_group__icontains=q)
        )
    return qs


@login_required
def export_employees_get(request):
    import pandas as pd
    from io import BytesIO
    from django.http import HttpResponse
    from .models import Employee
    import datetime
    
    qs = Employee.objects.all().order_by('name')
    qs = _filter_employees_queryset(qs, request.GET)
        
    data = []
    for i, emp in enumerate(qs, 1):
        data.append({
            'Sl No': i,
            'Employee ID': emp.emp_id or '',
            'Name': emp.name or '',
            'CID / National ID': emp.cid_number or '',
            'Blood Group': emp.blood_group or '',
            'Shift': emp.current_shift or 'General',
            'Nationality': emp.nationality or '',
            'Designation': emp.designation or '',
            'Department': emp.department or '',
            'Contractor / Agency': emp.contractor_agency or 'Direct',
            'Contact Info': emp.contact_info or '',
            'Passport No': emp.passport_details or '',
            'Work Permit No': emp.work_permit_no or '',
            'Permit Expiry': emp.work_permit_expiry.strftime("%Y-%m-%d") if emp.work_permit_expiry else '',
            'Joining Date': emp.joining_date.strftime("%Y-%m-%d") if emp.joining_date else '',
            'Status': emp.status or 'Active'
        })
        
    output = BytesIO()
    pd.DataFrame(data).to_excel(output, index=False)
    output.seek(0)
    response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    response['Content-Disposition'] = f'attachment; filename="Employees_Directory_{date_str}.xlsx"'
    return response


@login_required
def export_employees_pdf(request):
    from django.http import HttpResponse
    from django.utils import timezone
    from .models import Employee
    
    qs = Employee.objects.all().order_by('name')
    qs = _filter_employees_queryset(qs, request.GET)
        
    html = f"""
    <html>
    <head>
        <title>Employees Directory Report</title>
        <style>
            body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 20px; color: #333; }}
            h2 {{ text-align: center; color: #1e3a8a; margin-bottom: 5px; text-transform: uppercase; font-size: 18px; }}
            .sub-header {{ text-align: center; font-size: 11px; color: #64748b; margin-bottom: 15px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 11px; }}
            th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; }}
            th {{ background-color: #1e3a8a; color: white; font-weight: bold; text-transform: uppercase; font-size: 10px; }}
            tr:nth-child(even) {{ background-color: #f8fafc; }}
            .status-badge {{ padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 9px; text-transform: uppercase; }}
            .footer {{ text-align: right; margin-top: 15px; font-size: 10px; color: #666; }}
            @media print {{
                @page {{ size: landscape; margin: 1cm; }}
                button {{ display: none; }}
            }}
        </style>
    </head>
    <body>
        <h2>Employees Directory Report</h2>
        <div class="sub-header">Total Records: {qs.count()} | Generated on: {timezone.now().strftime('%d %b %Y, %I:%M %p')}</div>
        <table>
            <thead>
                <tr>
                    <th style="width:30px;">#</th>
                    <th>Emp ID</th>
                    <th>Name</th>
                    <th>CID / Nat ID</th>
                    <th>Designation</th>
                    <th>Department</th>
                    <th>Shift</th>
                    <th>Blood</th>
                    <th>Nationality</th>
                    <th>Contact</th>
                    <th>Agency</th>
                    <th>Permit / Pass</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
    """
    for i, emp in enumerate(qs, 1):
        permit_info = emp.work_permit_no or emp.passport_details or 'N/A'
        html += f"""
        <tr>
            <td>{i}</td>
            <td><b>{emp.emp_id or ''}</b></td>
            <td>{emp.name or ''}</td>
            <td>{emp.cid_number or '-'}</td>
            <td>{emp.designation or ''}</td>
            <td>{emp.department or ''}</td>
            <td>{emp.current_shift or '-'}</td>
            <td>{emp.blood_group or '-'}</td>
            <td>{emp.nationality or '-'}</td>
            <td>{emp.contact_info or '-'}</td>
            <td>{emp.contractor_agency or 'Direct'}</td>
            <td>{permit_info}</td>
            <td>{emp.status or 'Active'}</td>
        </tr>
        """
    html += f"""
            </tbody>
        </table>
        <div class="footer">Confidential • Generated by P&M Management System</div>
        <script>window.onload = function() {{ window.print(); }}</script>
    </body>
    </html>
    """
    return HttpResponse(html)


@csrf_exempt
@login_required
def export_employees_custom(request):
    import pandas as pd
    import json
    from django.http import HttpResponse, JsonResponse
    import io
    import datetime
    from django.utils import timezone
    from .models import Employee

    if request.method == "POST":
        try:
            data = json.loads(request.body)
            format_type = data.get('format', 'excel')
            report_title = data.get('report_title', 'Employees Directory Report')
            columns = data.get('columns', [])
            
            qs = Employee.objects.all().order_by('name')
            qs = _filter_employees_queryset(qs, data)
            
            if not columns:
                columns = [
                    "SL.NO", "EMPLOYEE ID", "NAME", "CID / NATIONAL ID", "DESIGNATION", 
                    "DEPARTMENT", "CURRENT SHIFT", "BLOOD GROUP", "NATIONALITY", 
                    "CONTACT INFO", "CONTRACTOR / AGENCY", "PASSPORT NO", "WORK PERMIT NO", 
                    "PERMIT EXPIRY", "JOINING DATE", "STATUS"
                ]
                
            report_data = []
            for i, emp in enumerate(qs, 1):
                row = {}
                if "SL.NO" in columns: row["SL.NO"] = i
                if "EMPLOYEE ID" in columns: row["EMPLOYEE ID"] = emp.emp_id or ""
                if "NAME" in columns: row["NAME"] = emp.name or ""
                if "CID / NATIONAL ID" in columns: row["CID / NATIONAL ID"] = emp.cid_number or ""
                if "DESIGNATION" in columns: row["DESIGNATION"] = emp.designation or ""
                if "DEPARTMENT" in columns: row["DEPARTMENT"] = emp.department or ""
                if "CURRENT SHIFT" in columns: row["CURRENT SHIFT"] = emp.current_shift or "General"
                if "BLOOD GROUP" in columns: row["BLOOD GROUP"] = emp.blood_group or ""
                if "NATIONALITY" in columns: row["NATIONALITY"] = emp.nationality or ""
                if "CONTACT INFO" in columns: row["CONTACT INFO"] = emp.contact_info or ""
                if "CONTRACTOR / AGENCY" in columns: row["CONTRACTOR / AGENCY"] = emp.contractor_agency or "Direct"
                if "PASSPORT NO" in columns: row["PASSPORT NO"] = emp.passport_details or ""
                if "WORK PERMIT NO" in columns: row["WORK PERMIT NO"] = emp.work_permit_no or ""
                if "PERMIT EXPIRY" in columns: row["PERMIT EXPIRY"] = emp.work_permit_expiry.strftime("%Y-%m-%d") if emp.work_permit_expiry else ""
                if "JOINING DATE" in columns: row["JOINING DATE"] = emp.joining_date.strftime("%Y-%m-%d") if emp.joining_date else ""
                if "STATUS" in columns: row["STATUS"] = emp.status or "Active"
                report_data.append(row)
                
            df = pd.DataFrame(report_data)
            
            if format_type == 'excel':
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Employees')
                output.seek(0)
                response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_title = "".join(c for c in report_title if c.isalnum() or c in (' ', '_', '-')).rstrip()
                response['Content-Disposition'] = f'attachment; filename="{safe_title}_{date_str}.xlsx"'
                return response
            else:
                # PDF View / Print HTML
                th_html = "".join([f"<th>{col}</th>" for col in columns])
                rows_html = ""
                for row in report_data:
                    tds = "".join([f"<td>{row.get(col, '')}</td>" for col in columns])
                    rows_html += f"<tr>{tds}</tr>"
                    
                html = f"""
                <html>
                <head>
                    <title>{report_title}</title>
                    <style>
                        body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 20px; color: #333; }}
                        h2 {{ text-align: center; color: #1e3a8a; margin-bottom: 5px; text-transform: uppercase; font-size: 18px; }}
                        .meta {{ text-align: center; font-size: 11px; color: #64748b; margin-bottom: 15px; }}
                        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 10px; }}
                        th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; }}
                        th {{ background-color: #1e3a8a; color: white; font-weight: bold; text-transform: uppercase; font-size: 9px; }}
                        tr:nth-child(even) {{ background-color: #f8fafc; }}
                        .footer {{ margin-top: 20px; font-size: 10px; text-align: right; color: #666; }}
                        @media print {{
                            @page {{ size: landscape; margin: 1cm; }}
                            button {{ display: none; }}
                        }}
                    </style>
                </head>
                <body>
                    <h2>{report_title}</h2>
                    <div class="meta">Total Records: {len(report_data)} | Generated: {timezone.now().strftime('%d %b %Y, %I:%M %p')}</div>
                    <table>
                        <thead>
                            <tr>{th_html}</tr>
                        </thead>
                        <tbody>
                            {rows_html}
                        </tbody>
                    </table>
                    <div class="footer">Confidential • Generated by P&M Department</div>
                    <script>window.onload = function() {{ window.print(); }}</script>
                </body>
                </html>
                """
                return HttpResponse(html)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
            
    return JsonResponse({'error': 'POST method required'}, status=405)


@csrf_exempt
def export_portal_data(request):
    import pandas as pd
    from io import BytesIO
    from django.http import HttpResponse, JsonResponse
    import datetime
    from django.utils import timezone
    from portal.models import Employee, EmployeeDocument, VehicleDocument, InsuranceDocument, DailyVehicleAllocation, EmployeeAttendance, OvertimeRecord
    from fleet.models import FleetVehicle, HiredVehicle, LubricationLog, TyreLog, SparePartTransaction, VehicleMovement

    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed"}, status=405)

    try:
        data = json.loads(request.body)
        format_type = data.get('format', 'excel')
        modules = data.get('modules', {})
        report_title = data.get('report_title', 'P&M Corporate Executive Report')
        global_start_date = data.get('start_date') or data.get('date_from')
        global_end_date = data.get('end_date') or data.get('date_to')
        today = datetime.date.today()

        # 1. DAILY DEPLOYMENT DATA
        deployment_data = []
        if 'deployment' in modules:
            conf = modules.get('deployment') or {}
            d_start = conf.get('start_date') or global_start_date
            d_end = conf.get('end_date') or global_end_date
            d_cat = conf.get('category')
            d_shift = conf.get('shift')
            d_zone = conf.get('zone')

            qs = DailyVehicleAllocation.objects.select_related('entered_by').all().order_by('-date', 'shift', 'vehicle_regn')
            if d_start: qs = qs.filter(date__gte=d_start)
            if d_end: qs = qs.filter(date__lte=d_end)
            if d_cat and d_cat != 'All': qs = qs.filter(category=d_cat)
            if d_shift and d_shift != 'All': qs = qs.filter(shift=d_shift)
            if d_zone and d_zone != 'All': qs = qs.filter(location_zone__icontains=d_zone)

            for i, a in enumerate(qs, 1):
                deployment_data.append({
                    "Sl No": i,
                    "Date": a.date.strftime("%Y-%m-%d") if a.date else "",
                    "Shift": a.shift or "Day",
                    "In Time": a.in_time or "",
                    "Out Time": a.out_time or "",
                    "Category": a.category or "",
                    "Vehicle Reg No": a.vehicle_regn or "",
                    "Driver / Operator": a.driver_name or "",
                    "Driver Emp ID": a.driver_emp_id or "",
                    "Driver Phone": a.driver_contact or "",
                    "Location / Zone": a.location_zone or "",
                    "Work Order No": a.work_order_no or "",
                    "Vendor / Owner": a.vendor or "",
                    "Remarks": a.remarks or ""
                })

        # 2. LUBRICANTS CONSUMED DATA
        lubricants_data = []
        if 'lubricants' in modules:
            conf = modules.get('lubricants') or {}
            l_start = conf.get('start_date') or global_start_date
            l_end = conf.get('end_date') or global_end_date
            l_veh = conf.get('vehicle_id')
            l_vendor = conf.get('vendor')
            l_oil = conf.get('oil_type')

            qs = LubricationLog.objects.select_related('vehicle').all().order_by('date', 'id')
            if l_start: qs = qs.filter(date__gte=l_start)
            if l_end: qs = qs.filter(date__lte=l_end)
            if l_veh: qs = qs.filter(vehicle_id=l_veh)
            if l_vendor: qs = qs.filter(vendor__iexact=l_vendor)
            if l_oil: qs = qs.filter(oil_type__iexact=l_oil)

            for i, log in enumerate(qs, 1):
                lubricants_data.append({
                    "Sl No": i,
                    "Date": log.date.strftime("%Y-%m-%d") if log.date else "",
                    "Location": log.location or "",
                    "Work Order No": log.work_order_no or "",
                    "Vendor": log.vendor or "",
                    "Vehicle Reg No": log.vehicle.regn if log.vehicle else "",
                    "Oil / Particulars": log.oil_type or "",
                    "Qty": float(log.qty) if log.qty else 0,
                    "Unit": log.unit or "L",
                    "Rate": float(log.rate) if log.rate else 0,
                    "Amount": float(log.amount) if log.amount else 0,
                    "Manpower Cost": float(log.manpower_cost) if log.manpower_cost else 0,
                    "Total Amount": float(log.total_amount) if log.total_amount else 0
                })

        # 3. TYRE FITMENT DATA
        tyre_data = []
        if 'tyre' in modules:
            conf = modules.get('tyre') or {}
            t_start = conf.get('start_date') or global_start_date
            t_end = conf.get('end_date') or global_end_date
            t_veh = conf.get('vehicle_id')
            t_vendor = conf.get('vendor')

            qs = TyreLog.objects.select_related('vehicle').all().order_by('date', 'id')
            if t_start: qs = qs.filter(date__gte=t_start)
            if t_end: qs = qs.filter(date__lte=t_end)
            if t_veh: qs = qs.filter(vehicle_id=t_veh)
            if t_vendor: qs = qs.filter(vendor__iexact=t_vendor)

            for i, log in enumerate(qs, 1):
                tyre_data.append({
                    "Sl No": i,
                    "Date": log.date.strftime("%Y-%m-%d") if log.date else "",
                    "Location": log.location or "",
                    "Work Order No": log.work_order_no or "",
                    "Vendor": log.vendor or "",
                    "Vehicle Reg No": log.vehicle.regn if log.vehicle else "",
                    "Punctures": log.punctures or 0,
                    "Big Patches": log.big_patches or 0,
                    "Small Patches": log.small_patches or 0,
                    "Nozzles": log.nozzles or 0,
                    "Valve Pin": log.valve_pin_number or "",
                    "Material Cost": float(log.material_cost) if log.material_cost else 0,
                    "Big Patches Cost": float(log.big_patches_cost) if log.big_patches_cost else 0,
                    "Small Patches Cost": float(log.small_patches_cost) if log.small_patches_cost else 0,
                    "Opening / Fitting": float(log.opening_fitting_cost) if log.opening_fitting_cost else 0,
                    "Total Amount": float(log.total_amount) if log.total_amount else 0
                })

        # 4. SPARE PARTS TRANSACTIONS DATA
        spares_data = []
        if 'spares' in modules:
            conf = modules.get('spares') or {}
            s_start = conf.get('start_date') or global_start_date
            s_end = conf.get('end_date') or global_end_date
            s_part = conf.get('part_id')
            s_veh = conf.get('vehicle_id')
            s_type = conf.get('transaction_type')

            qs = SparePartTransaction.objects.select_related('part', 'vehicle').filter(is_deleted=False).order_by('date', 'id')
            if s_start: qs = qs.filter(date__gte=s_start)
            if s_end: qs = qs.filter(date__lte=s_end)
            if s_part: qs = qs.filter(part_id=s_part)
            if s_veh: qs = qs.filter(vehicle_id=s_veh)
            if s_type: qs = qs.filter(transaction_type=s_type)

            for i, tx in enumerate(qs, 1):
                spares_data.append({
                    "Sl No": i,
                    "Date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "Type": tx.transaction_type or "",
                    "Part No": tx.part.part_number if tx.part else "",
                    "Part Name": tx.part.part_name if tx.part else "",
                    "Vehicle Reg No": tx.vehicle.regn if tx.vehicle else "",
                    "Qty": float(tx.quantity) if tx.quantity else 0,
                    "Unit": tx.part.unit if tx.part else "Pcs",
                    "Unit Rate": float(tx.rate) if tx.rate else 0,
                    "Total Amount": float(tx.total_amount) if tx.total_amount else 0,
                    "Supplier / Vendor": tx.supplier or "",
                    "Location": tx.location or "",
                    "WO No": tx.wo_no or "",
                    "Remarks": tx.remarks or ""
                })

        # 5. MACHINERY & VEHICLE FLEET DATA
        vehicles_data = []
        if 'vehicles' in modules:
            conf = modules.get('vehicles') or {}
            v_type = conf.get('type', 'all')

            if v_type in ['all', 'inhouse']:
                inhouse_qs = FleetVehicle.objects.all().order_by('regn')
                for i, v in enumerate(inhouse_qs, 1):
                    extra = v.extra_data or {}
                    vehicles_data.append({
                        "Sl No": len(vehicles_data) + 1,
                        "Ownership": "Inhouse Owned",
                        "Vehicle Reg No": v.regn or "",
                        "DNo / Code": v.dno or "",
                        "Model / Machinery": v.model_name or "",
                        "Department / Category": v.department.name if v.department else "",
                        "Vendor / Owner": extra.get('vendor', 'In-House'),
                        "Agreement Ref / WO": extra.get('work_order_no', ''),
                        "Status": extra.get('vehicle_status', 'Operational'),
                        "Chassis No": extra.get('chassis_no', ''),
                        "Engine No": extra.get('engine_no', '')
                    })

            if v_type in ['all', 'hired']:
                hired_qs = HiredVehicle.objects.all().order_by('regn')
                for i, h in enumerate(hired_qs, 1):
                    vehicles_data.append({
                        "Sl No": len(vehicles_data) + 1,
                        "Ownership": "Hired / Subcontractor",
                        "Vehicle Reg No": h.regn or "",
                        "DNo / Code": "-",
                        "Model / Machinery": h.equipment_type or "",
                        "Department / Category": h.hire_by or "",
                        "Vendor / Owner": h.owner_name or "",
                        "Agreement Ref / WO": h.agreement_ref or "",
                        "Status": h.status or h.contract_status or "Active",
                        "Chassis No": "",
                        "Engine No": ""
                    })

        # 6. EMPLOYEES DIRECTORY DATA
        employees_data = []
        if 'employees' in modules:
            emp_conf = modules.get('employees') or {}
            cols = emp_conf.get('columns', ['Emp ID', 'Name', 'Nationality', 'Designation', 'Department', 'Contact Info', 'Work Permit No', 'Permit Expiry', 'Status'])
            w_type = emp_conf.get('worker_type', 'all')
            emp_status_filter = emp_conf.get('status', 'all')
            emp_desig_filter = emp_conf.get('designation', 'all')

            qs = Employee.objects.all().order_by('emp_id', 'name')
            if w_type == 'national':
                qs = qs.filter(nationality__icontains='bhutan')
            elif w_type == 'foreign':
                qs = qs.exclude(nationality__icontains='bhutan')

            if emp_status_filter and emp_status_filter != 'all':
                qs = qs.filter(status=emp_status_filter)

            if emp_desig_filter and emp_desig_filter != 'all':
                qs = qs.filter(designation__icontains=emp_desig_filter)

            for i, emp in enumerate(qs, 1):
                row = {"Sl No": i}
                if 'Emp ID' in cols: row['Emp ID'] = emp.emp_id or ''
                if 'Name' in cols: row['Name'] = emp.name or ''
                if 'Nationality' in cols: row['Nationality'] = emp.nationality or ''
                if 'Designation' in cols: row['Designation'] = emp.designation or ''
                if 'Department' in cols: row['Department'] = emp.department or ''
                if 'Contact Info' in cols or 'Phone' in cols: row['Contact Info'] = emp.contact_info or ''
                if 'Work Permit No' in cols or 'Worker Type' in cols: row['Work Permit No'] = emp.work_permit_no or ''
                if 'Work Permit Expiry' in cols: row['Permit Expiry'] = emp.work_permit_expiry.strftime("%Y-%m-%d") if emp.work_permit_expiry else ''
                if 'Passport Details' in cols: row['Passport Details'] = emp.passport_details or ''
                if 'Contractor Agency' in cols: row['Contractor / Agency'] = emp.contractor_agency or ''
                if 'Status' in cols or 'Role' in cols: row['Status'] = emp.status or 'Active'
                employees_data.append(row)

        # 7. ATTENDANCE & MUSTER ROLL DATA
        attendance_data = []
        if 'attendance' in modules:
            conf = modules.get('attendance') or {}
            a_start = conf.get('start_date') or global_start_date
            a_end = conf.get('end_date') or global_end_date
            a_desig = conf.get('designation')
            a_status = conf.get('status')
            a_shift = conf.get('shift')

            qs = EmployeeAttendance.objects.select_related('employee').all().order_by('-date', 'employee__name')
            if a_start: qs = qs.filter(date__gte=a_start)
            if a_end: qs = qs.filter(date__lte=a_end)
            if a_desig and a_desig != 'all': qs = qs.filter(employee__designation__icontains=a_desig)
            if a_status and a_status != 'all': qs = qs.filter(status=a_status)
            if a_shift and a_shift != 'all': qs = qs.filter(shift__icontains=a_shift)

            for i, att in enumerate(qs, 1):
                attendance_data.append({
                    "Sl No": i,
                    "Date": att.date.strftime("%Y-%m-%d") if att.date else "",
                    "Emp ID": att.employee.emp_id if att.employee else "",
                    "Employee Name": att.employee.name if att.employee else "",
                    "Designation": att.employee.designation if att.employee else "",
                    "Department": att.employee.department if att.employee else "",
                    "Shift": att.shift or "General",
                    "Status": att.status or "Present",
                    "In Time": att.in_time.strftime("%H:%M") if att.in_time else "-",
                    "Out Time": att.out_time.strftime("%H:%M") if att.out_time else "-",
                    "Punch Source": att.punch_source or "MANUAL",
                    "Remarks": att.remarks or ""
                })

        # 8. OVERTIME LOGS DATA
        overtime_data = []
        if 'overtime' in modules:
            conf = modules.get('overtime') or {}
            o_start = conf.get('start_date') or global_start_date
            o_end = conf.get('end_date') or global_end_date
            o_desig = conf.get('designation')
            o_status = conf.get('status')

            qs = OvertimeRecord.objects.all().order_by('-date', 'employee_name')
            if o_start: qs = qs.filter(date__gte=o_start)
            if o_end: qs = qs.filter(date__lte=o_end)
            if o_desig and o_desig != 'all': qs = qs.filter(designation__icontains=o_desig)
            if o_status and o_status != 'all': qs = qs.filter(status=o_status)

            for i, ot in enumerate(qs, 1):
                overtime_data.append({
                    "Sl No": i,
                    "Date": ot.date.strftime("%Y-%m-%d") if ot.date else "",
                    "Emp ID": ot.emp_id_snapshot or (ot.employee.emp_id if ot.employee else ""),
                    "Employee Name": ot.employee_name or (ot.employee.name if ot.employee else ""),
                    "Designation": ot.designation or "",
                    "Department": ot.department or "",
                    "Shift": ot.shift or "Day",
                    "Location / Zone": ot.location_zone or "",
                    "Machine / Vehicle": ot.vehicle_regn or "",
                    "In Time": ot.in_time or "-",
                    "Out Time": ot.out_time or "-",
                    "OT Hours": float(ot.overtime_hours) if ot.overtime_hours else 0,
                    "OT Amount": float(ot.overtime_amount) if ot.overtime_amount else 0,
                    "Status": ot.status or "Pending",
                    "Work Description": ot.work_description or ""
                })

        # 9. DOCUMENTS & ALERTS DATA
        docs_data = []
        if 'documents' in modules:
            doc_conf = modules.get('documents') or {}
            doc_type = doc_conf.get('type', 'all')
            doc_exp_status = doc_conf.get('exp_status', 'all')

            def match_exp_filter(days):
                if doc_exp_status == 'expired' and days < 0: return True
                if doc_exp_status == 'critical' and 0 <= days <= 30: return True
                if doc_exp_status == 'warning' and 30 < days <= 90: return True
                if doc_exp_status == 'valid' and days > 90: return True
                if doc_exp_status == 'all': return True
                return False

            if doc_type in ['all', 'employee']:
                for emp in Employee.objects.filter(work_permit_expiry__isnull=False):
                    days_left = (emp.work_permit_expiry - today).days
                    if match_exp_filter(days_left):
                        status = "Expired" if days_left < 0 else ("Critical" if days_left <= 30 else ("Warning" if days_left <= 90 else "Valid"))
                        docs_data.append({
                            "Category": "Employee Work Permit",
                            "Entity Name": emp.name or "N/A",
                            "ID / Reg No": emp.emp_id or "N/A",
                            "Document Number": emp.work_permit_no or "N/A",
                            "Expiry Date": emp.work_permit_expiry.strftime("%Y-%m-%d"),
                            "Days Left": days_left,
                            "Status": status
                        })
                for doc in EmployeeDocument.objects.filter(expiry_date__isnull=False).select_related('employee'):
                    days_left = (doc.expiry_date - today).days
                    if match_exp_filter(days_left):
                        status = "Expired" if days_left < 0 else ("Critical" if days_left <= 30 else ("Warning" if days_left <= 90 else "Valid"))
                        docs_data.append({
                            "Category": "Employee Document",
                            "Entity Name": doc.employee.name if doc.employee else "N/A",
                            "ID / Reg No": doc.employee.emp_id if doc.employee else "N/A",
                            "Document Number": doc.document_number or "N/A",
                            "Expiry Date": doc.expiry_date.strftime("%Y-%m-%d"),
                            "Days Left": days_left,
                            "Status": status
                        })
            if doc_type in ['all', 'vehicle']:
                for doc in VehicleDocument.objects.filter(rc_expiry_date__isnull=False):
                    days_left = (doc.rc_expiry_date - today).days
                    if match_exp_filter(days_left):
                        status = "Expired" if days_left < 0 else ("Critical" if days_left <= 30 else ("Warning" if days_left <= 90 else "Valid"))
                        docs_data.append({
                            "Category": "Vehicle RC",
                            "Entity Name": doc.driver_operator or doc.registration_no or "Vehicle",
                            "ID / Reg No": doc.registration_no or "N/A",
                            "Document Number": doc.registration_no or "N/A",
                            "Expiry Date": doc.rc_expiry_date.strftime("%Y-%m-%d"),
                            "Days Left": days_left,
                            "Status": status
                        })
            if doc_type in ['all', 'insurance']:
                for doc in InsuranceDocument.objects.filter(expiry_date__isnull=False):
                    days_left = (doc.expiry_date - today).days
                    if match_exp_filter(days_left):
                        status = "Expired" if days_left < 0 else ("Critical" if days_left <= 30 else ("Warning" if days_left <= 90 else "Valid"))
                        docs_data.append({
                            "Category": "Insurance Policy",
                            "Entity Name": doc.registration_no or doc.driver_operator or "Vehicle",
                            "ID / Reg No": doc.registration_no or "N/A",
                            "Document Number": doc.policy_no or "N/A",
                            "Expiry Date": doc.expiry_date.strftime("%Y-%m-%d"),
                            "Days Left": days_left,
                            "Status": status
                        })
            docs_data.sort(key=lambda x: x['Days Left'])

        # 10. VEHICLE MOVEMENTS DATA
        movements_data = []
        if 'movements' in modules:
            conf = modules.get('movements') or {}
            m_start = conf.get('start_date') or global_start_date
            m_end = conf.get('end_date') or global_end_date
            m_veh = conf.get('vehicle_number') or conf.get('vehicle')

            qs = VehicleMovement.objects.select_related('vehicle', 'driver').filter(is_deleted=False).order_by('-movement_date', '-movement_time')
            if m_start: qs = qs.filter(movement_date__gte=m_start)
            if m_end: qs = qs.filter(movement_date__lte=m_end)
            if m_veh: qs = qs.filter(vehicle_number__icontains=m_veh)

            for i, m in enumerate(qs, 1):
                movements_data.append({
                    "Sl No": i,
                    "Date": m.movement_date.strftime("%Y-%m-%d") if m.movement_date else "",
                    "Time": m.movement_time.strftime("%H:%M") if m.movement_time else "",
                    "Vehicle Reg No": m.vehicle_number or "",
                    "Driver / Operator": m.driver_name or (m.driver.name if m.driver else ""),
                    "Shift": m.shift or "Day",
                    "Destination": m.destination or "",
                    "Purpose / Remarks": m.purpose or ""
                })

        # BUILD OUTPUT RESPONSE
        if format_type == 'excel':
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                has_sheet = False
                if 'deployment' in modules:
                    pd.DataFrame(deployment_data if deployment_data else [{"Msg": "No Deployment Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Daily Deployment')
                    has_sheet = True
                if 'lubricants' in modules:
                    pd.DataFrame(lubricants_data if lubricants_data else [{"Msg": "No Lubricants Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Lubricants Consumed')
                    has_sheet = True
                if 'tyre' in modules:
                    pd.DataFrame(tyre_data if tyre_data else [{"Msg": "No Tyre Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Tyre Debit Notes')
                    has_sheet = True
                if 'spares' in modules:
                    pd.DataFrame(spares_data if spares_data else [{"Msg": "No Spare Parts Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Spare Parts Ledger')
                    has_sheet = True
                if 'vehicles' in modules:
                    pd.DataFrame(vehicles_data if vehicles_data else [{"Msg": "No Machinery Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Machinery & Fleet')
                    has_sheet = True
                if 'employees' in modules:
                    pd.DataFrame(employees_data if employees_data else [{"Msg": "No Employees Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Employees Directory')
                    has_sheet = True
                if 'attendance' in modules:
                    pd.DataFrame(attendance_data if attendance_data else [{"Msg": "No Attendance Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Attendance Roll')
                    has_sheet = True
                if 'overtime' in modules:
                    pd.DataFrame(overtime_data if overtime_data else [{"Msg": "No Overtime Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Overtime Log')
                    has_sheet = True
                if 'documents' in modules:
                    pd.DataFrame(docs_data if docs_data else [{"Msg": "No Documents Matching Filters"}]).to_excel(writer, index=False, sheet_name='Documents & Alerts')
                    has_sheet = True
                if 'movements' in modules:
                    pd.DataFrame(movements_data if movements_data else [{"Msg": "No Movements Data Matching Filters"}]).to_excel(writer, index=False, sheet_name='Vehicle Movements')
                    has_sheet = True

                if not has_sheet:
                    pd.DataFrame([{"Msg": "No modules selected for export"}]).to_excel(writer, index=False, sheet_name='Empty')

            output.seek(0)
            response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            filename_str = report_title.replace(" ", "_") + f"_{timezone.now().strftime('%Y-%m-%d')}.xlsx"
            response['Content-Disposition'] = f'attachment; filename="{filename_str}"'
            return response

        elif format_type == 'pdf':
            html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>""" + report_title + """</title>
            <style>
                body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10px; margin: 15px; color: #1e293b; }
                table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
                th, td { border: 1px solid #cbd5e1; padding: 5px 8px; text-align: left; }
                th { background-color: #f1f5f9; font-weight: bold; color: #0f172a; }
                tr:nth-child(even) { background-color: #f8fafc; }
                h2 { color: #1e3a8a; border-bottom: 2px solid #1e3a8a; padding-bottom: 4px; margin-top:20px; font-size: 13px; }
                .header-banner { background: linear-gradient(135deg, #1e3a8a, #2563eb); color: white; padding: 14px 18px; border-radius: 8px; margin-bottom: 16px; }
                .header-banner h1 { margin:0; font-size: 18px; font-weight: 800; }
                .badge { padding: 2px 6px; border-radius: 10px; font-size: 9px; font-weight: bold; }
                .badge-Expired { background: #fee2e2; color: #ef4444; }
                .badge-Critical { background: #fee2e2; color: #ef4444; }
                .badge-Warning { background: #fef3c7; color: #d97706; }
                .badge-Valid { background: #d1fae5; color: #059669; }
                @media print {
                    @page { size: landscape; margin: 0.8cm; }
                    .no-print { display: none; }
                }
            </style></head><body>
            <div class="no-print" style="margin-bottom: 16px;">
                <button onclick="window.print()" style="padding: 8px 20px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; font-size: 13px;">
                    🖨️ Save as PDF / Print
                </button>
            </div>
            <div class="header-banner">
                <h1>""" + report_title + """</h1>
                <p style="margin:4px 0 0 0; opacity: 0.95; font-size: 11px;">Generated on: """ + timezone.now().strftime("%Y-%m-%d %H:%M") + """ | Plant & Machinery Central Audit</p>
            </div>"""

            def render_table(title, data_list):
                if not data_list: return f"<h2>{title}</h2><p><i>No data matching selected filters.</i></p>"
                res = f"<h2>{title} ({len(data_list)} records)</h2><table><tr>"
                keys = list(data_list[0].keys())
                for k in keys: res += f"<th>{k}</th>"
                res += "</tr>"
                for row in data_list[:300]:
                    res += "<tr>"
                    for k in keys:
                        val = str(row.get(k, ''))
                        if k == 'Status' and val in ['Expired', 'Critical', 'Warning', 'Valid']:
                            res += f"<td><span class='badge badge-{val}'>{val}</span></td>"
                        else:
                            res += f"<td>{val}</td>"
                    res += "</tr>"
                res += "</table>"
                return res

            if 'deployment' in modules: html += render_table("Daily Deployment Report", deployment_data)
            if 'lubricants' in modules: html += render_table("Lubricants Consumption Report", lubricants_data)
            if 'tyre' in modules: html += render_table("Tyre Debit Notes Report", tyre_data)
            if 'spares' in modules: html += render_table("Spare Parts Transactions", spares_data)
            if 'vehicles' in modules: html += render_table("Machinery & Fleet Catalog", vehicles_data)
            if 'employees' in modules: html += render_table("Employees Directory", employees_data)
            if 'attendance' in modules: html += render_table("Attendance & Muster Roll", attendance_data)
            if 'overtime' in modules: html += render_table("Overtime Records", overtime_data)
            if 'documents' in modules: html += render_table("Documents & Expiry Alerts", docs_data)
            if 'movements' in modules: html += render_table("Vehicle Movements Log", movements_data)

            html += "</body></html>"
            return HttpResponse(html, content_type='text/html')

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e)}, status=500)



def manager_vehicle_history_api(request, vehicle_id):
    if not (request.user.is_superuser or request.user.system_role == 'MANAGER'):
        return JsonResponse({"success": False, "error": "Unauthorized"})
    try:
        v = FleetVehicle.objects.get(id=vehicle_id)
        repairs = RepairLog.objects.filter(vehicle=v).order_by('-in_date')
        services = ServiceLog.objects.filter(vehicle=v).order_by('-service_date')
        
        history = []
        for r in repairs:
            history.append({
                "type": "REPAIR",
                "date": str(r.in_date),
                "complaint": r.complaint or "No complaint listed",
                "mechanic": r.mechanic or "Unassigned",
                "status": "Fixed" if r.out_date else "In Workshop"
            })
        for s in services:
            history.append({
                "type": "SERVICE",
                "date": str(s.service_date),
                "complaint": s.service_type,
                "mechanic": s.done_by or "Unknown",
                "status": s.status
            })
            
        history.sort(key=lambda x: x['date'], reverse=True)
        
        return JsonResponse({
            "success": True,
            "vehicle": {
                "dno": v.dno,
                "regn": v.regn,
                "model": v.model_name or "Unknown Model",
                "ownership": v.extra_data.get('ownership', 'In-House') if isinstance(v.extra_data, dict) else "In-House",
                "day_driver": v.extra_data.get('day_driver', 'Unassigned') if isinstance(v.extra_data, dict) else "Unassigned",
                "night_driver": v.extra_data.get('night_driver', 'Unassigned') if isinstance(v.extra_data, dict) else "Unassigned",
            },
            "history": history
        })
    except FleetVehicle.DoesNotExist:
        return JsonResponse({"success": False, "error": "Vehicle not found"})


@login_required
def request_otp(request):
    if request.method == 'POST':
        user = request.user
        if not user.email:
            return JsonResponse({'success': False, 'error': 'No email found for user'})
            
        otp = str(random.randint(100000, 999999))
        user.otp = otp
        user.save()
        
        # Send Email
        try:
            send_mail(
                'Password Reset Verification Code',
                f'Your 6-digit verification code is: {otp}. It is valid for this session only.',
                'admin@rigsarvajra.com',
                [user.email],
                fail_silently=False,
            )
        except Exception as e:
            print("Email error:", e)
            
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})

@login_required
def verify_otp(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            otp = data.get('otp')
            new_pwd = data.get('password')
            
            user = request.user
            if user.otp and str(user.otp) == str(otp):
                user.set_password(new_pwd)
                user.otp = None  # Clear the OTP
                user.save()
                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'error': 'Invalid OTP'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False})

@login_required
def request_otp(request):
    if request.method == 'POST':
        user = request.user
        if not user.email:
            return JsonResponse({'success': False, 'error': 'No email found for user'})
            
        otp = str(random.randint(100000, 999999))
        user.otp = otp
        user.save()
        
        # Send Email
        try:
            send_mail(
                'Password Reset Verification Code',
                f'Your 6-digit verification code is: {otp}. It is valid for this session only.',
                'admin@rigsarvajra.com',
                [user.email],
                fail_silently=False,
            )
        except Exception as e:
            print("Email error:", e)
            
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})

@login_required
def verify_otp(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            otp = data.get('otp')
            new_pwd = data.get('password')
            
            user = request.user
            if user.otp and str(user.otp) == str(otp):
                user.set_password(new_pwd)
                user.otp = None  # Clear the OTP
                user.save()
                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'error': 'Invalid OTP'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False})

@login_required
def _parse_insurance_date(raw):
    if not raw:
        return None
    import datetime, re
    if isinstance(raw, (datetime.date, datetime.datetime)):
        return raw.date() if isinstance(raw, datetime.datetime) else raw
    s = str(raw).strip()
    s = re.sub(r'^[a-zA-Z\s\-_/]+', '', s).strip()
    s = re.sub(r'/+', '/', s)
    m = re.search(r'(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})', s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        try:
            return datetime.date(y, mth, d)
        except Exception:
            pass
    m2 = re.search(r'(\d{1,2})[/.-](\d{2})(\d{2})', s)
    if m2:
        d, mth, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        if y < 100: y += 2000
        try:
            return datetime.date(y, mth, d)
        except Exception:
            pass
    return None

def _parse_rc_excel_date(raw):
    if not raw:
        return None
    import datetime
    if isinstance(raw, datetime.datetime):
        try:
            return datetime.date(raw.year, raw.day, raw.month)
        except Exception:
            return raw.date()
    elif isinstance(raw, datetime.date):
        try:
            return datetime.date(raw.year, raw.day, raw.month)
        except Exception:
            return raw
    return _parse_insurance_date(raw)

def document_alerts(request):
    # Handled at top of function, VehicleDocument, InsuranceDocument
    import datetime
    today = datetime.date.today()
    
    # Handle POST requests to add RC or Insurance records
    if request.method == 'POST':
        if 'add_vehicle_rc' in request.POST:
            VehicleDocument.objects.create(
                driver_operator=request.POST.get('driver_operator'),
                vehicle_type=request.POST.get('vehicle_type'),
                registration_no=request.POST.get('registration_no'),
                contact_no=request.POST.get('contact_no'),
                reports_to=request.POST.get('reports_to'),
                company=request.POST.get('company') or 'RVJV',
                chassis_no=request.POST.get('chassis_no'),
                engine_no=request.POST.get('engine_no'),
                rc_issued_on=request.POST.get('rc_issued_on') or None,
                rc_expiry_date=request.POST.get('rc_expiry_date') or None,
            )
            messages.success(request, 'Vehicle RC document added successfully!')
            return redirect('document_alerts')
        elif 'add_insurance' in request.POST:
            InsuranceDocument.objects.create(
                category=request.POST.get('category') or 'Pool Vehicle',
                driver_operator=request.POST.get('driver_operator') or None,
                contact_no=request.POST.get('contact_no') or None,
                night_driver=request.POST.get('night_driver') or None,
                night_contact=request.POST.get('night_contact') or None,
                vehicle_type=request.POST.get('vehicle_type') or None,
                registration_no=request.POST.get('registration_no') or None,
                company_supplier=request.POST.get('company_supplier') or 'RVJV',
                work_site=request.POST.get('work_site') or None,
                reports_to=request.POST.get('reports_to') or None,
                insurance_provider=request.POST.get('insurance_provider') or 'RICBL / Royal Insurance',
                policy_no=request.POST.get('policy_no') or None,
                issued_on=request.POST.get('issued_on') or None,
                expiry_date=request.POST.get('expiry_date') or None,
                remarks=request.POST.get('remarks') or None,
            )
            messages.success(request, 'Insurance record added successfully!')
            return redirect(f"{reverse('document_alerts')}?tab=insurance")

    # Build Vehicle RC Docs
    vehicle_docs = []
    for doc in VehicleDocument.objects.all():
        days_left = (doc.rc_expiry_date - today).days if doc.rc_expiry_date else 9999
        status = "Good"
        status_color = "#10b981"
        status_bg = "#d1fae5"
        if days_left < 0:
            status = "Expired"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
        elif days_left <= 30:
            status = "Critical"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
        elif days_left <= 90:
            status = "Warning"
            status_color = "#f59e0b"
            status_bg = "#fef3c7"
            
        vehicle_docs.append({
            'doc': doc,
            'days_left': days_left,
            'status': status,
            'status_color': status_color,
            'status_bg': status_bg
        })
    vehicle_docs.sort(key=lambda x: x['days_left'])

    # Build Insurance Docs
    insurance_docs = []
    ins_categories = set()
    ins_sites = set()
    
    ins_total = 0
    ins_valid = 0
    ins_warning = 0
    ins_critical = 0
    ins_expired = 0
    ins_pool_count = 0
    ins_equipment_count = 0
    ins_tanker_count = 0

    for doc in InsuranceDocument.objects.all():
        ins_total += 1
        cat = doc.category or 'Pool Vehicle'
        ins_categories.add(cat)
        if doc.work_site:
            ins_sites.add(doc.work_site)

        if 'pool' in cat.lower():
            ins_pool_count += 1
        elif 'equipment' in cat.lower() or 'machinery' in cat.lower():
            ins_equipment_count += 1
        elif 'tanker' in cat.lower():
            ins_tanker_count += 1

        days_left = (doc.expiry_date - today).days if doc.expiry_date else 9999
        status = "Good"
        status_color = "#10b981"
        status_bg = "#d1fae5"
        if days_left < 0:
            status = "Expired"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
            ins_expired += 1
        elif days_left <= 30:
            status = "Critical"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
            ins_critical += 1
        elif days_left <= 90:
            status = "Warning"
            status_color = "#f59e0b"
            status_bg = "#fef3c7"
            ins_warning += 1
        else:
            ins_valid += 1
            
        insurance_docs.append({
            'doc': doc,
            'days_left': days_left,
            'status': status,
            'status_color': status_color,
            'status_bg': status_bg
        })
    insurance_docs.sort(key=lambda x: x['days_left'])

    # Build Employee Work Permits / Documents
    documents = []
    for emp in Employee.objects.filter(work_permit_expiry__isnull=False):
        days_left = (emp.work_permit_expiry - today).days
        status = "Good"
        status_color = "#10b981"
        status_bg = "#d1fae5"
        if days_left < 0:
            status = "Expired"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
        elif days_left <= 30:
            status = "Critical"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
        elif days_left <= 90:
            status = "Warning"
            status_color = "#f59e0b"
            status_bg = "#fef3c7"
            
        documents.append({
            'id': emp.id,
            'source_type': 'employee_permit',
            'employee_name': emp.name,
            'employee_id': emp.emp_id,
            'doc_type': 'Work Permit',
            'doc_number': emp.work_permit_no or 'N/A',
            'expiry_date': emp.work_permit_expiry,
            'days_left': days_left,
            'status': status,
            'status_color': status_color,
            'status_bg': status_bg
        })
        
    for doc in EmployeeDocument.objects.filter(expiry_date__isnull=False).select_related('employee'):
        days_left = (doc.expiry_date - today).days
        status = "Good"
        status_color = "#10b981"
        status_bg = "#d1fae5"
        if days_left < 0:
            status = "Expired"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
        elif days_left <= 30:
            status = "Critical"
            status_color = "#ef4444"
            status_bg = "#fee2e2"
        elif days_left <= 90:
            status = "Warning"
            status_color = "#f59e0b"
            status_bg = "#fef3c7"
            
        documents.append({
            'id': doc.id,
            'source_type': 'employee_document',
            'employee_name': doc.employee.name if doc.employee else 'N/A',
            'employee_id': doc.employee.emp_id if doc.employee else 'N/A',
            'doc_type': doc.get_document_type_display() if hasattr(doc, 'get_document_type_display') else doc.document_type,
            'doc_number': doc.document_number or 'N/A',
            'expiry_date': doc.expiry_date,
            'days_left': days_left,
            'status': status,
            'status_color': status_color,
            'status_bg': status_bg
        })
        
    documents.sort(key=lambda x: x['days_left'])
    vehicle_rc_alerts_count = len([d for d in vehicle_docs if d['status'] in ['Expired', 'Critical', 'Warning']])
    insurance_alerts_count = len([d for d in insurance_docs if d['status'] in ['Expired', 'Critical', 'Warning']])
    employee_alerts_count = len([d for d in documents if d['status'] in ['Expired', 'Critical', 'Warning']])
    
    total_active = len([d for d in documents if d['status'] not in ['Expired']]) + len([d for d in vehicle_docs if d['status'] not in ['Expired']]) + len([d for d in insurance_docs if d['status'] not in ['Expired']])
    critical_count = len([d for d in documents if d['status'] in ['Expired', 'Critical']]) + len([d for d in vehicle_docs if d['status'] in ['Expired', 'Critical']]) + len([d for d in insurance_docs if d['status'] in ['Expired', 'Critical']])
    warning_count = len([d for d in documents if d['status'] == 'Warning']) + len([d for d in vehicle_docs if d['status'] == 'Warning']) + len([d for d in insurance_docs if d['status'] == 'Warning'])
    
    rc_total = len(vehicle_docs)
    rc_valid = len([d for d in vehicle_docs if d['status'] == 'Good'])
    rc_warning = len([d for d in vehicle_docs if d['status'] == 'Warning'])
    rc_critical = len([d for d in vehicle_docs if d['status'] == 'Critical'])
    rc_expired = len([d for d in vehicle_docs if d['status'] == 'Expired'])

    context = {
        'documents': documents,
        'vehicle_docs': vehicle_docs,
        'insurance_docs': insurance_docs,
        'vehicle_rc_alerts_count': vehicle_rc_alerts_count,
        'insurance_alerts_count': insurance_alerts_count,
        'employee_alerts_count': employee_alerts_count,
        'total_active': total_active,
        'critical_count': critical_count,
        'warning_count': warning_count,
        'rc_stats': {
            'total': rc_total,
            'valid': rc_valid,
            'warning': rc_warning,
            'critical': rc_critical,
            'expired': rc_expired,
        },
        'ins_stats': {
            'total': ins_total,
            'valid': ins_valid,
            'warning': ins_warning,
            'critical': ins_critical,
            'expired': ins_expired,
            'pool_count': ins_pool_count,
            'equipment_count': ins_equipment_count,
            'tanker_count': ins_tanker_count,
        },
        'ins_categories': sorted(list(ins_categories)),
        'ins_sites': sorted(list(ins_sites)),
        'employees_json': json.dumps([{'name': e.name or '', 'phone': e.contact_info or ''} for e in Employee.objects.all()])
    }
    
    return render(request, 'document_alerts.html', context)

@login_required
def edit_employee_action(request, emp_id):
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or 'dashboard'
    if request.method == 'POST':
        emp = get_object_or_404(Employee, id=emp_id)
        emp.emp_id = request.POST.get('emp_id')
        emp.name = request.POST.get('name')
        emp.nationality = request.POST.get('nationality')
        emp.designation = request.POST.get('designation')
        emp.department = request.POST.get('department')
        
        j_date = request.POST.get('joining_date')
        w_exp = request.POST.get('work_permit_expiry')
        
        emp.joining_date = j_date if j_date else None
        emp.work_permit_no = request.POST.get('work_permit_no')
        emp.work_permit_expiry = w_exp if w_exp else None
        
        emp.passport_details = request.POST.get('passport_details')
        emp.contact_info = request.POST.get('contact_info')
        emp.status = request.POST.get('status', 'Active')
        emp.contractor_agency = request.POST.get('contractor_agency')
        if request.POST.get('blood_group'):
            emp.blood_group = request.POST.get('blood_group')
        if request.POST.get('current_shift'):
            emp.current_shift = request.POST.get('current_shift')
        
        doc = request.FILES.get('document_upload')
        if doc:
            emp.document_upload = doc
            
        emp.entered_by = request.user
        emp.save()
        messages.success(request, f'Employee {emp.name} updated successfully!')
        
    if 'tab=' not in next_url:
        next_url = f"{next_url.rstrip('/')}/?tab=employees" if '?' not in next_url else f"{next_url}&tab=employees"
    return redirect(next_url)

@login_required
def delete_employee_action(request, emp_id):
    emp = get_object_or_404(Employee, id=emp_id)
    emp_name = emp.name or f"Employee #{emp.emp_id}"
    emp.delete()
    
    # Check if this is an AJAX request
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1' or 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({'success': True, 'message': f'{emp_name} deleted successfully!'})
        
    messages.success(request, f'{emp_name} deleted successfully!')
    next_url = request.META.get('HTTP_REFERER') or 'dashboard'
    if 'tab=' not in next_url:
        next_url = f"{next_url.rstrip('/')}/?tab=employees" if '?' not in next_url else f"{next_url}&tab=employees"
    return redirect(next_url)

@login_required
def add_employee_view(request):
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or 'dashboard'
    if request.method == 'POST':
        emp_id = request.POST.get('emp_id')
        name = request.POST.get('name')
        nationality = request.POST.get('nationality')
        designation = request.POST.get('designation')
        department = request.POST.get('department')
        joining_date = request.POST.get('joining_date')
        work_permit_no = request.POST.get('work_permit_no')
        work_permit_expiry = request.POST.get('work_permit_expiry')
        passport_details = request.POST.get('passport_details')
        contact_info = request.POST.get('contact_info')
        status = request.POST.get('status', 'Active')
        contractor_agency = request.POST.get('contractor_agency')
        blood_group = request.POST.get('blood_group', '')
        current_shift = request.POST.get('current_shift', 'General')
        
        doc = request.FILES.get('document_upload')
        
        if not joining_date: joining_date = None
        if not work_permit_expiry: work_permit_expiry = None
        
        Employee.objects.create(
            emp_id=emp_id,
            name=name,
            nationality=nationality,
            designation=designation,
            department=department,
            joining_date=joining_date,
            work_permit_no=work_permit_no,
            work_permit_expiry=work_permit_expiry,
            passport_details=passport_details,
            contact_info=contact_info,
            status=status,
            contractor_agency=contractor_agency,
            blood_group=blood_group,
            current_shift=current_shift,
            document_upload=doc,
            entered_by=request.user
        )
        messages.success(request, f'Employee {name} added successfully!')
    if 'tab=' not in next_url:
        next_url = f"{next_url.rstrip('/')}/?tab=employees" if '?' not in next_url else f"{next_url}&tab=employees"
    return redirect(next_url)

def delete_rc_action(request, doc_id):
    if request.method == 'POST':
        from portal.models import VehicleDocument
        doc = get_object_or_404(VehicleDocument, id=doc_id)
        doc.delete()
        messages.success(request, 'Vehicle RC deleted successfully!')
    return redirect('document_alerts')

def delete_insurance_action(request, doc_id):
    if request.method == 'POST':
        from portal.models import InsuranceDocument
        doc = get_object_or_404(InsuranceDocument, id=doc_id)
        doc.delete()
        messages.success(request, 'Insurance record deleted successfully!')
    return redirect(f"{reverse('document_alerts')}?tab=insurance")

@login_required
def delete_asset_permit_action(request, source_type, doc_id):
    if request.method == 'POST':
        if source_type == 'employee_permit':
            emp = get_object_or_404(Employee, id=doc_id)
            emp_name = emp.name
            emp.work_permit_no = None
            emp.work_permit_expiry = None
            emp.save()
            messages.success(request, f'Permit for {emp_name} deleted / cleared successfully!')
        elif source_type == 'employee_document':
            doc = get_object_or_404(EmployeeDocument, id=doc_id)
            emp_name = doc.employee.name if doc.employee else 'Record'
            doc.delete()
            messages.success(request, f'Document for {emp_name} deleted successfully!')
    return redirect(f"{reverse('document_alerts')}?tab=employees")

@login_required
def edit_asset_permit_action(request, source_type, doc_id):
    if request.method == 'POST':
        permit_no = request.POST.get('work_permit_no')
        exp_date = request.POST.get('work_permit_expiry')
        
        if source_type == 'employee_permit':
            emp = get_object_or_404(Employee, id=doc_id)
            emp.work_permit_no = permit_no
            emp.work_permit_expiry = exp_date if exp_date else None
            emp.save()
            messages.success(request, f'Permit for {emp.name} updated successfully!')
        elif source_type == 'employee_document':
            doc = get_object_or_404(EmployeeDocument, id=doc_id)
            doc.document_number = permit_no
            doc.expiry_date = exp_date if exp_date else None
            doc.save()
            emp_name = doc.employee.name if doc.employee else 'Document'
            messages.success(request, f'Document for {emp_name} updated successfully!')
    return redirect(f"{reverse('document_alerts')}?tab=employees")

def edit_rc_action(request, doc_id):
    if request.method == 'POST':
        from portal.models import VehicleDocument
        doc = get_object_or_404(VehicleDocument, id=doc_id)
        doc.driver_operator = request.POST.get('driver_operator')
        doc.vehicle_type = request.POST.get('vehicle_type')
        doc.registration_no = request.POST.get('registration_no')
        doc.contact_no = request.POST.get('contact_no')
        doc.reports_to = request.POST.get('reports_to')
        doc.company = request.POST.get('company') or 'RVJV'
        doc.chassis_no = request.POST.get('chassis_no')
        doc.engine_no = request.POST.get('engine_no')
        doc.rc_issued_on = request.POST.get('rc_issued_on') or None
        doc.rc_expiry_date = request.POST.get('rc_expiry_date') or None
        doc.save()
        messages.success(request, 'Vehicle RC updated successfully!')
    return redirect('document_alerts')

def edit_insurance_action(request, doc_id):
    if request.method == 'POST':
        from portal.models import InsuranceDocument
        doc = get_object_or_404(InsuranceDocument, id=doc_id)
        doc.category = request.POST.get('category') or doc.category
        doc.driver_operator = request.POST.get('driver_operator') or None
        doc.contact_no = request.POST.get('contact_no') or None
        doc.night_driver = request.POST.get('night_driver') or None
        doc.night_contact = request.POST.get('night_contact') or None
        doc.vehicle_type = request.POST.get('vehicle_type') or doc.vehicle_type
        doc.registration_no = request.POST.get('registration_no') or doc.registration_no
        doc.company_supplier = request.POST.get('company_supplier') or doc.company_supplier
        doc.work_site = request.POST.get('work_site') or None
        doc.reports_to = request.POST.get('reports_to') or None
        doc.insurance_provider = request.POST.get('insurance_provider') or None
        doc.policy_no = request.POST.get('policy_no') or None
        doc.issued_on = request.POST.get('issued_on') or None
        doc.expiry_date = request.POST.get('expiry_date') or None
        doc.remarks = request.POST.get('remarks') or None
        doc.save()
        messages.success(request, f'Insurance record for {doc.registration_no} updated successfully!')
    return redirect(f"{reverse('document_alerts')}?tab=insurance")


@login_required
def insurance_export_hub_view(request):
    return redirect(f"{reverse('document_alerts')}?tab=insurance&open_export=1")

@login_required
def export_insurance_excel(request):
    import datetime, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from django.http import HttpResponse
    from portal.models import InsuranceDocument
    
    today = datetime.date.today()
    category = request.GET.get('category', '').strip()
    status_filter = request.GET.get('status', '').strip()
    site_filter = request.GET.get('site', '').strip() or request.GET.get('work_site', '').strip()
    search = (request.GET.get('search', '') or request.GET.get('q', '')).strip().lower()
    from_date_str = (request.GET.get('from_date', '') or request.GET.get('date_from', '')).strip()
    to_date_str = (request.GET.get('to_date', '') or request.GET.get('date_to', '')).strip()

    qs = InsuranceDocument.objects.all()
    if category and category.lower() != 'all':
        qs = qs.filter(category__icontains=category)
    if site_filter and site_filter.lower() != 'all':
        qs = qs.filter(work_site__icontains=site_filter)
    if from_date_str:
        try:
            from_d = datetime.date.fromisoformat(from_date_str)
            qs = qs.filter(expiry_date__gte=from_d)
        except Exception: pass
    if to_date_str:
        try:
            to_d = datetime.date.fromisoformat(to_date_str)
            qs = qs.filter(expiry_date__lte=to_d)
        except Exception: pass

    docs = []
    for doc in qs:
        days_left = (doc.expiry_date - today).days if doc.expiry_date else 9999
        st = "Good"
        if days_left < 0:
            st = "Expired"
        elif days_left <= 30:
            st = "Critical"
        elif days_left <= 90:
            st = "Warning"
            
        if status_filter and status_filter.upper() != 'ALL' and st.upper() != status_filter.upper():
            continue
            
        if search:
            searchable = f"{doc.registration_no or ''} {doc.vehicle_type or ''} {doc.driver_operator or ''} {doc.night_driver or ''} {doc.work_site or ''} {doc.insurance_provider or ''} {doc.company_supplier or ''} {doc.policy_no or ''} {doc.category or ''}".lower()
            if search not in searchable:
                continue
                
        docs.append((doc, days_left, st))
        
    docs.sort(key=lambda x: x[1])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Insurance Register"

    # Styles
    title_font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
    title_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    sub_font = Font(name="Arial", size=10, bold=True, color="E0E7FF")
    header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    # Title Row
    ws.merge_cells('A1:N1')
    ws['A1'] = "RIGSAR - VAJRA JV"
    ws['A1'].font = title_font
    ws['A1'].fill = title_fill
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 28

    report_title = request.GET.get('report_title', '').strip() or "Vehicle & Equipment Insurance Register"
    ws.merge_cells('A2:N2')
    ws['A2'] = f"P&M Department • {report_title} | Generated: {today.strftime('%d-%b-%Y')} by {request.user.get_full_name() or request.user.username}"
    ws['A2'].font = sub_font
    ws['A2'].fill = title_fill
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 20

    # Headers
    headers = [
        "Sl No", "Category", "Reg. Number", "Vehicle / Equipment Type",
        "Day Driver / Operator", "Day Contact", "Night Driver / Operator", "Night Contact",
        "Reports To", "Work Site", "Insurance Provider", "Policy No",
        "Expiry Date", "Status (Days Left)"
    ]
    
    ws.append([]) # Empty row 3
    ws.append(headers)
    ws.row_dimensions[4].height = 24
    
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=4, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    # Data Rows
    row_num = 5
    for idx, (doc, days_left, st) in enumerate(docs, 1):
        days_str = f"{days_left}d" if days_left != 9999 else "N/A"
        st_display = f"{st} ({days_str})"
        
        row_data = [
            idx,
            doc.category or "Pool Vehicle",
            doc.registration_no or "-",
            doc.vehicle_type or "-",
            doc.driver_operator or "-",
            doc.contact_no or "-",
            doc.night_driver or "-",
            doc.night_contact or "-",
            doc.reports_to or "-",
            doc.work_site or "-",
            doc.insurance_provider or "RICBL / Royal Insurance",
            doc.policy_no or "-",
            doc.expiry_date.strftime('%d-%b-%Y') if doc.expiry_date else "N/A",
            st_display
        ]
        ws.append(row_data)
        ws.row_dimensions[row_num].height = 20
        
        for col_idx in range(1, len(row_data) + 1):
            c = ws.cell(row=row_num, column=col_idx)
            c.border = thin_border
            c.font = Font(name="Arial", size=9)
            if col_idx in [1, 13, 14]:
                c.alignment = Alignment(horizontal='center', vertical='center')
            else:
                c.alignment = Alignment(horizontal='left', vertical='center')
                
        status_cell = ws.cell(row=row_num, column=14)
        if st == 'Expired':
            status_cell.fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
            status_cell.font = Font(name="Arial", size=9, bold=True, color="991B1B")
        elif st == 'Critical':
            status_cell.fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
            status_cell.font = Font(name="Arial", size=9, bold=True, color="DC2626")
        elif st == 'Warning':
            status_cell.fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
            status_cell.font = Font(name="Arial", size=9, bold=True, color="D97706")
        else:
            status_cell.fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
            status_cell.font = Font(name="Arial", size=9, bold=True, color="059669")
            
        row_num += 1

    from openpyxl.utils import get_column_letter
    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        max_len = 0
        for r in range(4, row_num):
            val = str(ws.cell(row=r, column=col_idx).value or '')
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Insurance_Register_RVJV_{today.strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response

@login_required
def export_insurance_pdf(request):
    import datetime
    from portal.models import InsuranceDocument
    
    today = datetime.date.today()
    category = request.GET.get('category', '').strip()
    status_filter = request.GET.get('status', '').strip()
    site_filter = request.GET.get('site', '').strip() or request.GET.get('work_site', '').strip()
    search = (request.GET.get('search', '') or request.GET.get('q', '')).strip().lower()
    from_date_str = (request.GET.get('from_date', '') or request.GET.get('date_from', '')).strip()
    to_date_str = (request.GET.get('to_date', '') or request.GET.get('date_to', '')).strip()

    report_title = request.GET.get('report_title', '').strip() or "Vehicle & Equipment Insurance Register"
    prep_name = request.GET.get('prepared_by_name', '').strip() or "Sonam Penjor"
    prep_desig = request.GET.get('prepared_by_desig', '').strip() or "Fleet / P&M Supervisor"
    ver_name = request.GET.get('verified_by_name', '').strip() or "Kinley Wangdi"
    ver_desig = request.GET.get('verified_by_desig', '').strip() or "Mechanical Engineer"
    appr_name = request.GET.get('approved_by_name', '').strip() or "Karma Tshering"
    appr_desig = request.GET.get('approved_by_desig', '').strip() or "Project Manager"

    qs = InsuranceDocument.objects.all()
    if category and category.lower() != 'all':
        qs = qs.filter(category__icontains=category)
    if site_filter and site_filter.lower() != 'all':
        qs = qs.filter(work_site__icontains=site_filter)
    if from_date_str:
        try:
            from_d = datetime.date.fromisoformat(from_date_str)
            qs = qs.filter(expiry_date__gte=from_d)
        except Exception: pass
    if to_date_str:
        try:
            to_d = datetime.date.fromisoformat(to_date_str)
            qs = qs.filter(expiry_date__lte=to_d)
        except Exception: pass

    insurance_list = []
    tot_valid = 0
    tot_warning = 0
    tot_critical = 0
    tot_expired = 0

    for doc in qs:
        days_left = (doc.expiry_date - today).days if doc.expiry_date else 9999
        st = "Good"
        badge_color = "#10b981"
        badge_bg = "#d1fae5"
        if days_left < 0:
            st = "Expired"
            badge_color = "#991b1b"
            badge_bg = "#fee2e2"
            tot_expired += 1
        elif days_left <= 30:
            st = "Critical"
            badge_color = "#dc2626"
            badge_bg = "#fee2e2"
            tot_critical += 1
        elif days_left <= 90:
            st = "Warning"
            badge_color = "#d97706"
            badge_bg = "#fef3c7"
            tot_warning += 1
        else:
            tot_valid += 1

        if status_filter and status_filter.upper() != 'ALL' and st.upper() != status_filter.upper():
            continue

        if search:
            searchable = f"{doc.registration_no or ''} {doc.vehicle_type or ''} {doc.driver_operator or ''} {doc.night_driver or ''} {doc.work_site or ''} {doc.insurance_provider or ''} {doc.company_supplier or ''} {doc.policy_no or ''} {doc.category or ''}".lower()
            if search not in searchable:
                continue

        insurance_list.append({
            'doc': doc,
            'days_left': days_left,
            'status': st,
            'badge_color': badge_color,
            'badge_bg': badge_bg
        })

    insurance_list.sort(key=lambda x: x['days_left'])

    stats = {
        'total': len(insurance_list),
        'valid': tot_valid,
        'warning': tot_warning,
        'critical': tot_critical,
        'expired': tot_expired
    }

    context = {
        'insurance_list': insurance_list,
        'stats': stats,
        'today_str': today.strftime('%d-%b-%Y'),
        'category_filter': category or 'All Categories',
        'status_filter': status_filter or 'All Statuses',
        'report_title': report_title,
        'prepared_by_name': prep_name,
        'prepared_by_desig': prep_desig,
        'verified_by_name': ver_name,
        'verified_by_desig': ver_desig,
        'approved_by_name': appr_name,
        'approved_by_desig': appr_desig,
    }
    return render(request, 'insurance_pdf.html', context)

@login_required
def import_insurance_excel_action(request):
    import openpyxl
    from portal.models import InsuranceDocument
    
    if request.method != 'POST':
        return redirect('document_alerts')
        
    excel_file = request.FILES.get('excel_file')
    if not excel_file:
        messages.error(request, 'Please select an Excel file to upload.')
        return redirect(f"{reverse('document_alerts')}?tab=insurance")

    try:
        wb = openpyxl.load_workbook(excel_file, data_only=True)
        records = []

        # Check for multi-sheet Insurance Lists format
        sheet_names = [s.strip() for s in wb.sheetnames]
        
        if 'Company Pool Vehicle' in sheet_names or 'Equipments' in [s.strip() for s in sheet_names] or 'Tanker' in sheet_names:
            for name in wb.sheetnames:
                if 'pool' in name.lower():
                    ws1 = wb[name]
                    for r in list(ws1.iter_rows(values_only=True))[1:]:
                        if not any(r) or len(r) < 8 or not r[7]: continue
                        sl, day_d, day_c, night_d, night_c, v_type, rep_to, reg_no = r[:8]
                        comp = r[8] if len(r) > 8 else 'RVJV'
                        site = r[9] if len(r) > 9 else None
                        ins_raw = r[10] if len(r) > 10 else None
                        exp = _parse_insurance_date(ins_raw)
                        records.append({
                            'category': 'Pool Vehicle',
                            'vehicle_type': str(v_type).strip() if v_type else 'Pool Vehicle',
                            'registration_no': str(reg_no).strip(),
                            'driver_operator': str(day_d).strip() if day_d else None,
                            'contact_no': str(day_c).strip() if day_c else None,
                            'night_driver': str(night_d).strip() if (night_d and str(night_d).lower() not in ['no', 'none', '-']) else None,
                            'night_contact': str(night_c).strip() if night_c else None,
                            'reports_to': str(rep_to).strip() if rep_to else None,
                            'company_supplier': str(comp).strip() if comp else 'RVJV',
                            'work_site': str(site).strip() if site else None,
                            'expiry_date': exp,
                            'insurance_provider': 'RICBL / Royal Insurance'
                        })

                elif 'equip' in name.lower():
                    ws2 = wb[name]
                    for r in list(ws2.iter_rows(values_only=True))[1:]:
                        if len(r) > 2 and r[2] and str(r[2]).strip() not in ['Registration Number', 'None', '', 'N/A']:
                            exp = _parse_insurance_date(r[4]) if len(r) > 4 else None
                            records.append({
                                'category': 'Equipment / Machinery',
                                'vehicle_type': str(r[1]).strip() if len(r) > 1 and r[1] else 'Heavy Equipment',
                                'registration_no': str(r[2]).strip(),
                                'driver_operator': None,
                                'contact_no': None,
                                'night_driver': None,
                                'night_contact': None,
                                'reports_to': None,
                                'company_supplier': str(r[3]).strip() if len(r) > 3 and r[3] else 'RVJV',
                                'work_site': None,
                                'expiry_date': exp,
                                'insurance_provider': 'RICBL / Royal Insurance'
                            })
                        if len(r) > 8 and r[8] and str(r[8]).strip() not in ['Registration Number', 'None', '', 'N/A']:
                            exp = _parse_insurance_date(r[10]) if len(r) > 10 else None
                            records.append({
                                'category': 'Equipment / Machinery',
                                'vehicle_type': str(r[7]).strip() if len(r) > 7 and r[7] else 'Heavy Equipment',
                                'registration_no': str(r[8]).strip(),
                                'driver_operator': None,
                                'contact_no': None,
                                'night_driver': None,
                                'night_contact': None,
                                'reports_to': None,
                                'company_supplier': str(r[9]).strip() if len(r) > 9 and r[9] else 'RVJV',
                                'work_site': None,
                                'expiry_date': exp,
                                'insurance_provider': 'RICBL / Royal Insurance'
                            })

                elif 'tanker' in name.lower():
                    ws3 = wb[name]
                    for r in list(ws3.iter_rows(values_only=True))[1:]:
                        if not any(r) or len(r) < 3 or not r[2]: continue
                        sl, model, reg_no = r[:3]
                        supp = r[3] if len(r) > 3 else 'RVJV'
                        ins_raw = r[4] if len(r) > 4 else None
                        exp = _parse_insurance_date(ins_raw)
                        records.append({
                            'category': 'Tanker',
                            'vehicle_type': str(model).strip() if model else 'Tanker',
                            'registration_no': str(reg_no).strip(),
                            'driver_operator': None,
                            'contact_no': None,
                            'night_driver': None,
                            'night_contact': None,
                            'reports_to': None,
                            'company_supplier': str(supp).strip() if supp else 'RVJV',
                            'work_site': None,
                            'expiry_date': exp,
                            'insurance_provider': 'RICBL / Royal Insurance'
                        })
        else:
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if rows:
                for r in rows[1:]:
                    if not any(r): continue
                    reg_no = str(r[2] if len(r) > 2 else r[0] or '').strip()
                    if not reg_no or reg_no.lower() in ['registration no', 'reg no', 'none', '-']:
                        continue
                    exp = _parse_insurance_date(r[13] if len(r) > 13 else (r[5] if len(r) > 5 else None))
                    records.append({
                        'category': str(r[1]).strip() if len(r) > 1 and r[1] else 'Pool Vehicle',
                        'registration_no': reg_no,
                        'vehicle_type': str(r[3]).strip() if len(r) > 3 and r[3] else 'Vehicle',
                        'driver_operator': str(r[4]).strip() if len(r) > 4 and r[4] else None,
                        'contact_no': str(r[5]).strip() if len(r) > 5 and r[5] else None,
                        'night_driver': str(r[6]).strip() if len(r) > 6 and r[6] else None,
                        'night_contact': str(r[7]).strip() if len(r) > 7 and r[7] else None,
                        'reports_to': str(r[8]).strip() if len(r) > 8 and r[8] else None,
                        'company_supplier': str(r[9]).strip() if len(r) > 9 and r[9] else 'RVJV',
                        'work_site': str(r[10]).strip() if len(r) > 10 and r[10] else None,
                        'insurance_provider': str(r[11]).strip() if len(r) > 11 and r[11] else 'RICBL / Royal Insurance',
                        'policy_no': str(r[12]).strip() if len(r) > 12 and r[12] else None,
                        'expiry_date': exp
                    })

        created_cnt = 0
        updated_cnt = 0
        for item in records:
            reg = item['registration_no']
            doc, created = InsuranceDocument.objects.get_or_create(
                registration_no=reg,
                defaults={
                    'driver_operator': item.get('driver_operator'),
                    'contact_no': item.get('contact_no'),
                    'night_driver': item.get('night_driver'),
                    'night_contact': item.get('night_contact'),
                    'vehicle_type': item.get('vehicle_type'),
                    'reports_to': item.get('reports_to'),
                    'insurance_provider': item.get('insurance_provider'),
                    'company_supplier': item.get('company_supplier'),
                    'work_site': item.get('work_site'),
                    'category': item.get('category'),
                    'expiry_date': item.get('expiry_date'),
                }
            )
            if not created:
                for k, v in item.items():
                    if v is not None:
                        setattr(doc, k, v)
                doc.save()
                updated_cnt += 1
            else:
                created_cnt += 1

        messages.success(request, f'Successfully imported {len(records)} insurance records ({created_cnt} new, {updated_cnt} updated)!')
    except Exception as e:
        messages.error(request, f'Failed to import Excel file: {str(e)}')

    return redirect(f"{reverse('document_alerts')}?tab=insurance")



@login_required
def snooze_notification(request, notif_id):
    if request.method == 'POST':
        try:
            n = Notification.objects.get(id=notif_id, user=request.user)
            n.created_at = timezone.now() + timedelta(days=1)
            n.save()
            return JsonResponse({'success': True})
        except: pass
    return JsonResponse({'success': False})

@login_required
def get_unread_notifications(request):
    user = request.user
    is_manager = user.system_role == 'MANAGER' or user.is_superuser
    
    # Non-managers should NOT see ALERT (document expiry) notifications
    if not is_manager:
        # Clean up any stale ALERT notifications mistakenly created for this user
        Notification.objects.filter(user=user, notification_type='ALERT').delete()
        notifications = Notification.objects.filter(user=user).exclude(notification_type='ALERT').order_by('-created_at')[:20]
    else:
        notifications = Notification.objects.filter(user=user).order_by('-created_at')[:20]
    
    data = []
    for n in notifications:
        data.append({
            'id': n.id,
            'title': n.title,
            'message': n.message,
            'type': n.notification_type,
            'time': n.created_at.strftime('%d %b %H:%M'),
            'link': n.link or '',
            'is_read': n.is_read
        })
    
    if is_manager:
        unread_count = Notification.objects.filter(user=user, is_read=False).count()
    else:
        unread_count = Notification.objects.filter(user=user, is_read=False).exclude(notification_type='ALERT').count()
    return JsonResponse({'notifications': data, 'unread_count': unread_count})

@login_required
def mark_notifications_read(request):
    if request.method == 'POST':
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})


@login_required
def delete_all_notifications(request):
    if request.method == 'POST':
        Notification.objects.filter(user=request.user).delete()
        messages.success(request, 'All notifications cleared successfully.')
        return redirect('notifications')
    return JsonResponse({'success': False}, status=400)

@login_required
def update_notification_prefs(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            request.user.notification_prefs = {
                'docs': data.get('docs', True),
                'docs_days': data.get('docs_days', 90),
                'breakdowns': data.get('breakdowns', True),
                'system': data.get('system', True)
            }
            request.user.save()
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False})


@login_required
def notifications_page(request):
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'notifications.html', {'notifications': notifications})



@login_required
def document_edit_view(request, doc_type, doc_id):
    if doc_type == 'vehicle':
        obj = get_object_or_404(VehicleDocument, id=doc_id)
        FormClass = VehicleDocumentForm
        title = 'Edit Vehicle Document (RC)'
    elif doc_type == 'insurance':
        obj = get_object_or_404(InsuranceDocument, id=doc_id)
        FormClass = InsuranceDocumentForm
        title = 'Edit Insurance Document'
    else:
        return redirect('dashboard')

    if request.method == 'POST':
        form = FormClass(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Document updated successfully!')
            return redirect('dashboard')
    else:
        form = FormClass(instance=obj)

    return render(request, 'document_edit.html', {
        'form': form,
        'title': title
    })


@login_required
def notification_detail_view(request, notif_id):
    notif = get_object_or_404(Notification, id=notif_id, user=request.user)
    if not notif.is_read:
        notif.is_read = True
        notif.save()
    
    return render(request, 'notification_detail.html', {'notification': notif})

@login_required
@csrf_exempt
def import_documents_view(request):
    if request.method == 'POST' and request.FILES.get('excel_file'):
        try:
            import openpyxl
            excel_file = request.FILES['excel_file']
            doc_type = request.POST.get('doc_type')

            wb = openpyxl.load_workbook(excel_file, data_only=True)
            count = 0

            if doc_type == 'rc':
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                if rows:
                    header = [str(c or '').lower() for c in rows[0]]
                    is_rc_info_fmt = any('chassis' in c for c in header) or ('vehicle type' in header and 'number' in header)
                    for r in rows[1:]:
                        if not any(r): continue
                        if is_rc_info_fmt:
                            v_type = str(r[1] or '').strip() if len(r) > 1 and r[1] else None
                            reg_no = str(r[2] or '').strip() if len(r) > 2 and r[2] else None
                            if not reg_no or reg_no.lower() in ['number', 'registration no', 'reg no', 'none', '-']: continue
                            comp = str(r[3] or 'RVJV').strip() if len(r) > 3 and r[3] else 'RVJV'
                            chassis = str(r[4] or '').strip() if len(r) > 4 and r[4] else None
                            engine = str(r[5] or '').strip() if len(r) > 5 and r[5] else None
                            exp = _parse_rc_excel_date(r[6]) if len(r) > 6 else None
                            if chassis:
                                VehicleDocument.objects.update_or_create(
                                    registration_no=reg_no,
                                    chassis_no=chassis,
                                    defaults={
                                        'vehicle_type': v_type,
                                        'company': comp,
                                        'engine_no': engine,
                                        'rc_expiry_date': exp,
                                        'driver_operator': 'P&M Fleet / RVJV',
                                        'reports_to': 'P&M Incharge'
                                    }
                                )
                            else:
                                VehicleDocument.objects.update_or_create(
                                    registration_no=reg_no,
                                    defaults={
                                        'vehicle_type': v_type,
                                        'company': comp,
                                        'chassis_no': chassis,
                                        'engine_no': engine,
                                        'rc_expiry_date': exp,
                                        'driver_operator': 'P&M Fleet / RVJV',
                                        'reports_to': 'P&M Incharge'
                                    }
                                )
                        else:
                            reg_no = str(r[0] or '').strip()
                            if not reg_no or reg_no.lower() in ['registration no', 'reg no', 'none', '-']: continue
                            VehicleDocument.objects.create(
                                registration_no=reg_no,
                                driver_operator=str(r[1] or '') if len(r) > 1 and r[1] else None,
                                vehicle_type=str(r[2] or '') if len(r) > 2 and r[2] else None,
                                contact_no=str(r[3] or '') if len(r) > 3 and r[3] else None,
                                reports_to=str(r[4] or '') if len(r) > 4 and r[4] else None,
                                rc_issued_on=_parse_insurance_date(r[5]) if len(r) > 5 else None,
                                rc_expiry_date=_parse_insurance_date(r[6]) if len(r) > 6 else None
                            )
                        count += 1
            elif doc_type == 'insurance':
                sheet_names = [s.strip() for s in wb.sheetnames]
                records = []
                if 'Company Pool Vehicle' in sheet_names or 'Equipments' in [s.strip() for s in sheet_names] or 'Tanker' in sheet_names:
                    for name in wb.sheetnames:
                        if 'pool' in name.lower():
                            ws1 = wb[name]
                            for r in list(ws1.iter_rows(values_only=True))[1:]:
                                if not any(r) or len(r) < 8 or not r[7]: continue
                                sl, day_d, day_c, night_d, night_c, v_type, rep_to, reg_no = r[:8]
                                comp = r[8] if len(r) > 8 else 'RVJV'
                                site = r[9] if len(r) > 9 else None
                                ins_raw = r[10] if len(r) > 10 else None
                                exp = _parse_insurance_date(ins_raw)
                                records.append({
                                    'category': 'Pool Vehicle',
                                    'vehicle_type': str(v_type).strip() if v_type else 'Pool Vehicle',
                                    'registration_no': str(reg_no).strip(),
                                    'driver_operator': str(day_d).strip() if day_d else None,
                                    'contact_no': str(day_c).strip() if day_c else None,
                                    'night_driver': str(night_d).strip() if (night_d and str(night_d).lower() not in ['no', 'none', '-']) else None,
                                    'night_contact': str(night_c).strip() if night_c else None,
                                    'reports_to': str(rep_to).strip() if rep_to else None,
                                    'company_supplier': str(comp).strip() if comp else 'RVJV',
                                    'work_site': str(site).strip() if site else None,
                                    'expiry_date': exp,
                                    'insurance_provider': 'RICBL / Royal Insurance'
                                })

                        elif 'equip' in name.lower():
                            ws2 = wb[name]
                            for r in list(ws2.iter_rows(values_only=True))[1:]:
                                if len(r) > 2 and r[2] and str(r[2]).strip() not in ['Registration Number', 'None', '', 'N/A']:
                                    exp = _parse_insurance_date(r[4]) if len(r) > 4 else None
                                    records.append({
                                        'category': 'Equipment / Machinery',
                                        'vehicle_type': str(r[1]).strip() if len(r) > 1 and r[1] else 'Heavy Equipment',
                                        'registration_no': str(r[2]).strip(),
                                        'driver_operator': None,
                                        'contact_no': None,
                                        'night_driver': None,
                                        'night_contact': None,
                                        'reports_to': None,
                                        'company_supplier': str(r[3]).strip() if len(r) > 3 and r[3] else 'RVJV',
                                        'work_site': None,
                                        'expiry_date': exp,
                                        'insurance_provider': 'RICBL / Royal Insurance'
                                    })
                                if len(r) > 8 and r[8] and str(r[8]).strip() not in ['Registration Number', 'None', '', 'N/A']:
                                    exp = _parse_insurance_date(r[10]) if len(r) > 10 else None
                                    records.append({
                                        'category': 'Equipment / Machinery',
                                        'vehicle_type': str(r[7]).strip() if len(r) > 7 and r[7] else 'Heavy Equipment',
                                        'registration_no': str(r[8]).strip(),
                                        'driver_operator': None,
                                        'contact_no': None,
                                        'night_driver': None,
                                        'night_contact': None,
                                        'reports_to': None,
                                        'company_supplier': str(r[9]).strip() if len(r) > 9 and r[9] else 'RVJV',
                                        'work_site': None,
                                        'expiry_date': exp,
                                        'insurance_provider': 'RICBL / Royal Insurance'
                                    })

                        elif 'tanker' in name.lower():
                            ws3 = wb[name]
                            for r in list(ws3.iter_rows(values_only=True))[1:]:
                                if not any(r) or len(r) < 3 or not r[2]: continue
                                sl, model, reg_no = r[:3]
                                supp = r[3] if len(r) > 3 else 'RVJV'
                                ins_raw = r[4] if len(r) > 4 else None
                                exp = _parse_insurance_date(ins_raw)
                                records.append({
                                    'category': 'Tanker',
                                    'vehicle_type': str(model).strip() if model else 'Tanker',
                                    'registration_no': str(reg_no).strip(),
                                    'driver_operator': None,
                                    'contact_no': None,
                                    'night_driver': None,
                                    'night_contact': None,
                                    'reports_to': None,
                                    'company_supplier': str(supp).strip() if supp else 'RVJV',
                                    'work_site': None,
                                    'expiry_date': exp,
                                    'insurance_provider': 'RICBL / Royal Insurance'
                                })
                else:
                    ws = wb.active
                    rows = list(ws.iter_rows(values_only=True))
                    if rows:
                        for r in rows[1:]:
                            if not any(r): continue
                            reg_no = str(r[2] if len(r) > 2 else r[0] or '').strip()
                            if not reg_no or reg_no.lower() in ['registration no', 'reg no', 'none', '-']:
                                continue
                            exp = _parse_insurance_date(r[13] if len(r) > 13 else (r[5] if len(r) > 5 else None))
                            records.append({
                                'category': str(r[1]).strip() if len(r) > 1 and r[1] else 'Pool Vehicle',
                                'registration_no': reg_no,
                                'vehicle_type': str(r[3]).strip() if len(r) > 3 and r[3] else 'Vehicle',
                                'driver_operator': str(r[4]).strip() if len(r) > 4 and r[4] else None,
                                'contact_no': str(r[5]).strip() if len(r) > 5 and r[5] else None,
                                'night_driver': str(r[6]).strip() if len(r) > 6 and r[6] else None,
                                'night_contact': str(r[7]).strip() if len(r) > 7 and r[7] else None,
                                'reports_to': str(r[8]).strip() if len(r) > 8 and r[8] else None,
                                'company_supplier': str(r[9]).strip() if len(r) > 9 and r[9] else 'RVJV',
                                'work_site': str(r[10]).strip() if len(r) > 10 and r[10] else None,
                                'insurance_provider': str(r[11]).strip() if len(r) > 11 and r[11] else 'RICBL / Royal Insurance',
                                'policy_no': str(r[12]).strip() if len(r) > 12 and r[12] else None,
                                'expiry_date': exp
                            })

                for item in records:
                    reg = item['registration_no']
                    doc, created = InsuranceDocument.objects.get_or_create(
                        registration_no=reg,
                        defaults={
                            'driver_operator': item.get('driver_operator'),
                            'contact_no': item.get('contact_no'),
                            'night_driver': item.get('night_driver'),
                            'night_contact': item.get('night_contact'),
                            'vehicle_type': item.get('vehicle_type'),
                            'reports_to': item.get('reports_to'),
                            'insurance_provider': item.get('insurance_provider'),
                            'company_supplier': item.get('company_supplier'),
                            'work_site': item.get('work_site'),
                            'category': item.get('category'),
                            'expiry_date': item.get('expiry_date'),
                        }
                    )
                    if not created:
                        for k, v in item.items():
                            if v is not None:
                                setattr(doc, k, v)
                        doc.save()
                    count += 1

            return JsonResponse({'status': 'success', 'message': f'{count} records imported / updated successfully.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})

    return JsonResponse({'status': 'error', 'message': 'Invalid request'})

@login_required
@csrf_exempt
def ocr_extract_view(request):
    if request.method == 'POST' and request.FILES.get('document_image'):
        try:
            image_file = request.FILES['document_image']
            doc_type = request.POST.get('doc_type', 'rc')

            # TODO: Integrate real OCR engine here (Google Vision, Gemini, or Tesseract)
            # For now, returning mock extracted data for the user to preview
            mock_data = {}
            if doc_type == 'rc':
                mock_data = {
                    'registration_no': 'DL-1C-AA-1234',
                    'issued_on': '2022-05-10',
                    'expiry_date': '2037-05-09'
                }
            elif doc_type == 'insurance':
                mock_data = {
                    'registration_no': 'DL-1C-AA-1234',
                    'insurance_provider': 'Mock General Insurance',
                    'policy_no': 'POL-9988776655',
                    'issued_on': '2023-01-01',
                    'expiry_date': '2024-01-01'
                }

            return JsonResponse({'status': 'success', 'data': mock_data, 'message': 'Simulated OCR successful. Please review the data.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})

    return JsonResponse({'status': 'error', 'message': 'Invalid request'})


from django.core.paginator import Paginator
from datetime import datetime, date

from .models import DailyVehicleAllocation

def _get_machinery_category(model_or_eq, fallback='Other'):
    text = (str(model_or_eq) or '').lower()
    if any(k in text for k in ['scania', 'p440', 'g440', 'dumper', 'tipper', 'trailer', '10 wheeler', '12 wheeler', '18 wheeler']):
        return 'Scania'
    if 'excavator' in text:
        return 'Excavator'
    if 'grader' in text:
        return 'Grader'
    if any(k in text for k in ['compactor', 'roller']):
        return 'Compactor'
    return fallback or 'Other'

def _sync_allocation_to_deployment(date_val, category):
    """Recalculate Zone Day/Night counts for a date and category from DailyVehicleAllocation."""
    cat_label_map = {
        'Scania': 'Dumper (10 W)',
        'Excavator': 'Excavator',
        'Grader': 'Motor Grader',
        'Compactor': 'Compactor 22T',
    }
    machinery_label = cat_label_map.get(category, category)
    
    allocs = DailyVehicleAllocation.objects.filter(date=date_val, category=category)
    
    z12_d = allocs.filter(location_zone__icontains='Zone 1', shift='Day').count()
    z12_n = allocs.filter(location_zone__icontains='Zone 1', shift='Night').count()
    
    z34_d = allocs.filter(location_zone__icontains='Zone 3', shift='Day').count()
    z34_n = allocs.filter(location_zone__icontains='Zone 3', shift='Night').count()
    
    ba_d = allocs.filter(location_zone__icontains='Borrow', shift='Day').count()
    ba_n = allocs.filter(location_zone__icontains='Borrow', shift='Night').count()
    
    ca_d = allocs.filter(location_zone__icontains='Culvert', shift='Day').count()
    ca_n = allocs.filter(location_zone__icontains='Culvert', shift='Night').count()
    
    bp_d = allocs.filter(location_zone__icontains='Batching', shift='Day').count()
    bp_n = allocs.filter(location_zone__icontains='Batching', shift='Night').count()
    
    cp_d = allocs.filter(location_zone__icontains='Crushing', shift='Day').count()
    cp_n = allocs.filter(location_zone__icontains='Crushing', shift='Night').count()
    
    rm_d = allocs.filter(location_zone__icontains='Road', shift='Day').count()
    rm_n = allocs.filter(location_zone__icontains='Road', shift='Night').count()
    
    if allocs.exists():
        dep, _ = DailyDeployment.objects.get_or_create(date=date_val, machinery=machinery_label)
        dep.zone_1_2_day = z12_d
        dep.zone_1_2_night = z12_n
        dep.zone_3_4_day = z34_d
        dep.zone_3_4_night = z34_n
        dep.borrow_area_day = ba_d
        dep.borrow_area_night = ba_n
        dep.culvert_area_day = ca_d
        dep.culvert_area_night = ca_n
        dep.batching_plant_day = bp_d
        dep.batching_plant_night = bp_n
        dep.crushing_plant_day = cp_d
        dep.crushing_plant_night = cp_n
        dep.road_maint_day = rm_d
        dep.road_maint_night = rm_n
        dep.save()

@login_required
def deployment_view(request):
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser:
        if getattr(request.user, 'assigned_modules', None) and 'daily_deployment' not in request.user.assigned_modules:
            return redirect('dashboard')

    from .models import Employee
    from fleet.models import FleetVehicle, HiredVehicle
    import datetime
    import re
    
    def _clean_r(s):
        return re.sub(r'[^A-Za-z0-9]', '', str(s or '')).upper()

    today = datetime.date.today()
    selected_date_str = request.GET.get('date', '')
    selected_category = request.GET.get('category', 'All')
    selected_shift = request.GET.get('shift', 'All')
    view_mode = request.GET.get('view', 'allocations')
    
    # Captain Role-Based Department Restrictions
    user_captain_cat = getattr(request.user, 'captain_category', 'All') or 'All'
    is_captain_restricted = False
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser and user_captain_cat != 'All':
        is_captain_restricted = True
        selected_category = user_captain_cat

    # Filter allocations
    allocations_qs = DailyVehicleAllocation.objects.select_related('entered_by', 'driver', 'vehicle').all()
    if selected_date_str:
        try:
            d_obj = datetime.datetime.strptime(selected_date_str, '%Y-%m-%d').date()
            allocations_qs = allocations_qs.filter(date=d_obj)
        except Exception:
            pass
    if is_captain_restricted:
        allocations_qs = allocations_qs.filter(category=user_captain_cat)
    elif selected_category and selected_category != 'All':
        allocations_qs = allocations_qs.filter(category=selected_category)
    if selected_shift and selected_shift != 'All':
        allocations_qs = allocations_qs.filter(shift=selected_shift)
        
    allocations = allocations_qs.order_by('-date', 'shift', 'category', 'vehicle_regn')[:500]
    
    # Master Grid (Rohan Sir's view)
    deployments = DailyDeployment.objects.select_related('entered_by').all().order_by('-date')[:2000]
    machinery_list = DailyDeployment.objects.values_list('machinery', flat=True).distinct().order_by('machinery')
    available_dates = DailyDeployment.objects.values_list('date', flat=True).distinct().order_by('-date')
    
    # Build fast map of Hired vehicles by clean regn
    hired_map = {}
    for h in HiredVehicle.objects.all():
        cr = _clean_r(h.regn)
        if cr:
            hired_map[cr] = h
            
    # Fetch vehicles categorized for fast JS lookup
    vehicles_by_cat = {
        'All': [],
        'Scania': [],
        'Excavator': [],
        'Grader': [],
        'Compactor': [],
        'Other': []
    }
    seen_regns = set()
    for v in FleetVehicle.objects.filter(is_active=True).order_by('regn'):
        if not v.regn:
            continue
        cr = _clean_r(v.regn)
        seen_regns.add(cr)
        hired = hired_map.get(cr)
        v_extra = v.extra_data if isinstance(v.extra_data, dict) else {}
        stored_cat = v_extra.get('category') or (v.department.name if v.department else '')
        cat = stored_cat if stored_cat in vehicles_by_cat else _get_machinery_category(v.model_name or (hired.equipment_type if hired else ''))
        target_cat = cat if cat in vehicles_by_cat else 'Other'
        wo = (hired.agreement_ref if hired else '') or v_extra.get('contract_ref', '') or ''
        vendor_name = (hired.owner_name if hired else '') or v_extra.get('owner_name', '') or ''
        v_item = {
            'id': v.id,
            'regn': v.regn,
            'model': v.model_name or (hired.equipment_type if hired else ''),
            'wo': wo,
            'vendor': vendor_name,
            'category': target_cat
        }
        vehicles_by_cat['All'].append(v_item)
        vehicles_by_cat[target_cat].append(v_item)
        
    # Also include any Hired vehicles that might not be in FleetVehicle
    for cr, h in hired_map.items():
        if cr not in seen_regns and h.regn:
            seen_regns.add(cr)
            cat = _get_machinery_category(h.equipment_type or '')
            target_cat = cat if cat in vehicles_by_cat else 'Other'
            h_item = {
                'id': 0,
                'regn': h.regn,
                'model': h.equipment_type or '',
                'wo': h.agreement_ref or '',
                'vendor': h.owner_name or '',
                'category': target_cat
            }
            vehicles_by_cat['All'].append(h_item)
            vehicles_by_cat[target_cat].append(h_item)

    # Also include any vehicles from historical DailyVehicleAllocation
    for a in DailyVehicleAllocation.objects.values('vehicle_regn', 'category', 'work_order_no', 'vendor').distinct():
        v_r = (a.get('vehicle_regn') or '').strip()
        if not v_r or v_r == 'Unassigned':
            continue
        cr = _clean_r(v_r)
        if cr and cr not in seen_regns:
            seen_regns.add(cr)
            cat = a.get('category') or 'Other'
            target_cat = cat if cat in vehicles_by_cat else 'Other'
            a_item = {
                'id': 0,
                'regn': v_r,
                'model': cat,
                'wo': a.get('work_order_no') or '',
                'vendor': a.get('vendor') or '',
                'category': target_cat
            }
            vehicles_by_cat['All'].append(a_item)
            vehicles_by_cat[target_cat].append(a_item)
        
    # Fetch drivers categorized for fast dropdowns
    drivers_by_cat = {
        'All': [],
        'Scania': [],
        'Excavator': [],
        'Grader': [],
        'Compactor': [],
        'Other': []
    }
    seen_drivers = set()
    for e in Employee.objects.filter(status='Active').order_by('name'):
        clean_name = (e.name or '').strip()
        if not clean_name:
            continue
        seen_drivers.add(clean_name.lower())
        cat = 'Other'
        dept_lower = (e.department or '').lower() + ' ' + (e.designation or '').lower()
        if 'scania' in dept_lower or 'dumper' in dept_lower or 'tipper' in dept_lower:
            cat = 'Scania'
        elif 'excavator' in dept_lower:
            cat = 'Excavator'
        elif 'grader' in dept_lower:
            cat = 'Grader'
        elif 'compactor' in dept_lower or 'roller' in dept_lower:
            cat = 'Compactor'
            
        target_cat = cat if cat in drivers_by_cat else 'Other'
        d_item = {
            'id': e.id,
            'name': clean_name,
            'emp_id': e.emp_id or '',
            'phone': e.contact_info or '',
            'desig': e.designation or 'Operator',
            'category': target_cat
        }
        drivers_by_cat['All'].append(d_item)
        drivers_by_cat[target_cat].append(d_item)

    # Also include any drivers from past DailyVehicleAllocation
    for a in DailyVehicleAllocation.objects.values('driver_name', 'driver_emp_id', 'driver_contact', 'category').distinct():
        d_name = (a.get('driver_name') or '').strip()
        if d_name and d_name != 'Unassigned' and d_name.lower() not in seen_drivers:
            seen_drivers.add(d_name.lower())
            cat = a.get('category') or 'Other'
            target_cat = cat if cat in drivers_by_cat else 'Other'
            d_item = {
                'id': 0,
                'name': d_name,
                'emp_id': a.get('driver_emp_id') or '',
                'phone': a.get('driver_contact') or '',
                'desig': 'Driver',
                'category': target_cat
            }
            drivers_by_cat['All'].append(d_item)
            drivers_by_cat[target_cat].append(d_item)
        
    # Metrics
    today_allocs = DailyVehicleAllocation.objects.filter(date=today)
    if is_captain_restricted:
        today_allocs = today_allocs.filter(category=user_captain_cat)
    metrics = {
        'total_today': today_allocs.count(),
        'day_shift': today_allocs.filter(shift='Day').count(),
        'night_shift': today_allocs.filter(shift='Night').count(),
        'scania_count': today_allocs.filter(category='Scania').count(),
        'excavator_count': today_allocs.filter(category='Excavator').count(),
        'grader_count': today_allocs.filter(category='Grader').count(),
        'compactor_count': today_allocs.filter(category='Compactor').count(),
    }

    return render(request, 'deployment.html', {
        'allocations': allocations,
        'deployments': deployments,
        'machinery_list': machinery_list,
        'available_dates': available_dates,
        'vehicles_by_cat': vehicles_by_cat,
        'drivers_by_cat': drivers_by_cat,
        'selected_category': selected_category,
        'selected_shift': selected_shift,
        'selected_date': selected_date_str,
        'view_mode': view_mode,
        'today': today,
        'metrics': metrics,
        'is_captain_restricted': is_captain_restricted,
        'user_captain_category': user_captain_cat,
        'restricted_drivers': drivers_by_cat.get(user_captain_cat, []) if is_captain_restricted else [],
    })

@login_required
def api_add_vehicle_allocation(request):
    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.POST.get('is_ajax') == '1' or 'application/json' in request.headers.get('Accept', '')
        try:
            import datetime
            import re
            d_str = request.POST.get('date') or datetime.date.today().strftime('%Y-%m-%d')
            if isinstance(d_str, str):
                try:
                    d_obj = datetime.datetime.strptime(d_str, '%Y-%m-%d').date()
                except Exception:
                    d_obj = datetime.date.today()
            else:
                d_obj = d_str
            shift = request.POST.get('shift', 'Day')
            category = request.POST.get('category', 'Scania')
            
            # Enforce Captain restriction if applicable
            user_captain_cat = getattr(request.user, 'captain_category', 'All') or 'All'
            if request.user.system_role != 'MANAGER' and not request.user.is_superuser and user_captain_cat != 'All':
                category = user_captain_cat
                
            vehicle_regn = (request.POST.get('vehicle_regn') or '').strip()
            driver_name = (request.POST.get('driver_name') or '').strip()
            driver_emp_id = (request.POST.get('driver_emp_id') or '').strip()
            driver_contact = (request.POST.get('driver_contact') or '').strip()
            location_zone = request.POST.get('location_zone', 'Zone 1 & 2')
            work_order_no = (request.POST.get('work_order_no') or '').strip()
            vendor = (request.POST.get('vendor') or '').strip()
            in_time = (request.POST.get('in_time') or '').strip()
            out_time = (request.POST.get('out_time') or '').strip()
            remarks = (request.POST.get('remarks') or '').strip()
            
            if not in_time:
                in_time = datetime.datetime.now().strftime('%H:%M')
            
            if not vehicle_regn:
                vehicle_regn = 'Unassigned'
                
            from fleet.models import FleetVehicle, HiredVehicle, get_or_create_fleet_vehicle
            from .models import Employee
            
            def _clean_r(s):
                return re.sub(r'[^A-Za-z0-9]', '', str(s or '')).upper()
                
            v_clean = _clean_r(vehicle_regn)
            vehicle_obj = None
            if v_clean and v_clean != 'UNASSIGNED':
                # If WO or Vendor are empty, try auto-fill before creating/saving
                if not work_order_no or not vendor:
                    for h in HiredVehicle.objects.all():
                        if _clean_r(h.regn) == v_clean:
                            if not work_order_no: work_order_no = h.agreement_ref or ''
                            if not vendor: vendor = h.owner_name or ''
                            break
                            
                vehicle_obj = get_or_create_fleet_vehicle(
                    vehicle_regn,
                    vendor=vendor,
                    work_order_no=work_order_no,
                    location=location_zone,
                    category=category
                )
                    
            driver_obj = None
            if driver_name and driver_name.strip() and driver_name.strip() != 'Unassigned':
                clean_dname = driver_name.strip()
                driver_obj = Employee.objects.filter(name__iexact=clean_dname).first()
                if not driver_obj:
                    try:
                        driver_obj = Employee.objects.create(
                            name=clean_dname,
                            emp_id=driver_emp_id,
                            contact_info=driver_contact,
                            designation='Driver / Operator',
                            department=category,
                            status='Active'
                        )
                    except Exception:
                        pass
                else:
                    d_updated = False
                    if driver_emp_id and not driver_obj.emp_id:
                        driver_obj.emp_id = driver_emp_id
                        d_updated = True
                    if driver_contact and not driver_obj.contact_info:
                        driver_obj.contact_info = driver_contact
                        d_updated = True
                    if d_updated:
                        try:
                            driver_obj.save()
                        except Exception:
                            pass
            
            alloc = DailyVehicleAllocation.objects.create(
                date=d_obj,
                shift=shift,
                category=category,
                vehicle=vehicle_obj,
                vehicle_regn=vehicle_regn,
                driver=driver_obj,
                driver_name=driver_name or 'Unassigned',
                driver_emp_id=driver_emp_id,
                driver_contact=driver_contact,
                location_zone=location_zone,
                work_order_no=work_order_no,
                vendor=vendor,
                out_time=out_time,
                in_time=in_time,
                remarks=remarks,
                entered_by=request.user,
            )
            
            # Auto-sync rollup to DailyDeployment
            _sync_allocation_to_deployment(d_obj, category)
            log_activity(request.user, 'CREATE', 'Vehicle Allocation', f"Allocated vehicle {vehicle_regn} ({category}) to {driver_name or 'Unassigned'} - Zone: {location_zone}, Shift: {shift}", request)
            
            if is_ajax:
                return JsonResponse({
                    'status': 'success',
                    'message': f'Allocation for {vehicle_regn} ({driver_name}) saved successfully!',
                    'allocation': {
                        'id': alloc.id,
                        'date': alloc.date.strftime('%d %b %Y'),
                        'raw_date': alloc.date.strftime('%Y-%m-%d'),
                        'shift': alloc.shift,
                        'category': alloc.category,
                        'vehicle_regn': alloc.vehicle_regn,
                        'model_name': alloc.vehicle.model_name if alloc.vehicle else '',
                        'driver_name': alloc.driver_name,
                        'driver_emp_id': alloc.driver_emp_id or '',
                        'driver_contact': alloc.driver_contact or '',
                        'location_zone': alloc.location_zone,
                        'work_order_no': alloc.work_order_no or '-',
                        'vendor': alloc.vendor or '-',
                        'out_time': alloc.out_time or '',
                        'in_time': alloc.in_time or '',
                        'created_at_time': localtime(alloc.created_at).strftime('%I:%M %p') if alloc.created_at else '',
                        'created_at_str': localtime(alloc.created_at).strftime('%d %b, %I:%M %p') if alloc.created_at else '',
                        'entered_by': alloc.entered_by.full_name or alloc.entered_by.username if alloc.entered_by else 'System'
                    }
                })
            
            messages.success(request, f'Allocation for {vehicle_regn} ({driver_name}) saved successfully!')
        except Exception as e:
            if is_ajax:
                return JsonResponse({'status': 'error', 'message': f'Error saving: {str(e)}'}, status=500)
            messages.error(request, f'Error saving allocation: {str(e)}')
            
    return redirect('deployment')

@login_required
def api_edit_vehicle_allocation(request, alloc_id):
    alloc = get_object_or_404(DailyVehicleAllocation, id=alloc_id)
    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.POST.get('is_ajax') == '1' or 'application/json' in request.headers.get('Accept', '')
        try:
            import datetime, re
            from fleet.models import FleetVehicle, HiredVehicle, get_or_create_fleet_vehicle
            from .models import Employee
            
            old_date = alloc.date
            old_cat = alloc.category
            
            d = request.POST.get('date')
            d_obj = datetime.datetime.strptime(d, '%Y-%m-%d').date() if d else alloc.date
            shift = request.POST.get('shift', alloc.shift)
            category = request.POST.get('category', alloc.category)
            vehicle_regn = (request.POST.get('vehicle_regn') or alloc.vehicle_regn or '').strip().upper()
            driver_name = (request.POST.get('driver_name') or '').strip()
            driver_emp_id = (request.POST.get('driver_emp_id') or '').strip()
            driver_contact = (request.POST.get('driver_contact') or '').strip()
            location_zone = request.POST.get('location_zone', alloc.location_zone)
            work_order_no = (request.POST.get('work_order_no') or '').strip()
            vendor = (request.POST.get('vendor') or '').strip()
            out_time = (request.POST.get('out_time') or '').strip()
            in_time = (request.POST.get('in_time') or '').strip()
            remarks = (request.POST.get('remarks') or '').strip()
            
            vehicle_obj = None
            if vehicle_regn and vehicle_regn != 'UNASSIGNED':
                vehicle_obj = get_or_create_fleet_vehicle(
                    vehicle_regn,
                    vendor=vendor,
                    work_order_no=work_order_no,
                    location=location_zone,
                    category=category
                )

            driver_obj = None
            if driver_name and driver_name.strip() and driver_name.strip() != 'Unassigned':
                clean_dname = driver_name.strip()
                driver_obj = Employee.objects.filter(name__iexact=clean_dname).first()
                if not driver_obj:
                    try:
                        driver_obj = Employee.objects.create(
                            name=clean_dname,
                            emp_id=driver_emp_id,
                            contact_info=driver_contact,
                            designation='Driver / Operator',
                            department=category,
                            status='Active'
                        )
                    except Exception:
                        pass
                else:
                    d_updated = False
                    if driver_emp_id and not driver_obj.emp_id:
                        driver_obj.emp_id = driver_emp_id
                        d_updated = True
                    if driver_contact and not driver_obj.contact_info:
                        driver_obj.contact_info = driver_contact
                        d_updated = True
                    if d_updated:
                        try:
                            driver_obj.save()
                        except Exception:
                            pass

            alloc.date = d_obj
            alloc.shift = shift
            alloc.category = category
            alloc.vehicle = vehicle_obj
            alloc.vehicle_regn = vehicle_regn
            alloc.driver = driver_obj
            alloc.driver_name = driver_name
            alloc.driver_emp_id = driver_emp_id
            alloc.driver_contact = driver_contact
            alloc.location_zone = location_zone
            alloc.work_order_no = work_order_no
            alloc.vendor = vendor
            alloc.out_time = out_time
            alloc.in_time = in_time
            alloc.remarks = remarks
            alloc.save()
            
            _sync_allocation_to_deployment(old_date, old_cat)
            if old_date != d_obj or old_cat != category:
                _sync_allocation_to_deployment(d_obj, category)
                
            log_activity(request.user, 'UPDATE', 'Vehicle Allocation', f"Updated allocation #{alloc_id} for {vehicle_regn} ({category}) - Driver: {driver_name or 'Unassigned'}", request)
                
            if is_ajax:
                return JsonResponse({
                    'status': 'success',
                    'message': 'Allocation updated successfully!',
                    'allocation': {
                        'id': alloc.id,
                        'date': alloc.date.strftime('%d %b %Y'),
                        'raw_date': alloc.date.strftime('%Y-%m-%d'),
                        'shift': alloc.shift,
                        'category': alloc.category,
                        'vehicle_regn': alloc.vehicle_regn,
                        'model_name': alloc.vehicle.model_name if alloc.vehicle else '',
                        'driver_name': alloc.driver_name,
                        'driver_emp_id': alloc.driver_emp_id or '',
                        'driver_contact': alloc.driver_contact or '',
                        'location_zone': alloc.location_zone,
                        'work_order_no': alloc.work_order_no or '-',
                        'vendor': alloc.vendor or '-',
                        'out_time': alloc.out_time or '',
                        'in_time': alloc.in_time or '',
                        'remarks': alloc.remarks or '-',
                        'created_at_time': localtime(alloc.created_at).strftime('%I:%M %p') if alloc.created_at else '',
                        'created_at_str': localtime(alloc.created_at).strftime('%d %b, %I:%M %p') if alloc.created_at else '',
                        'entered_by': alloc.entered_by.full_name or alloc.entered_by.username if alloc.entered_by else 'System'
                    }
                })
            messages.success(request, 'Allocation updated successfully!')
        except Exception as e:
            if is_ajax:
                return JsonResponse({'status': 'error', 'message': f'Error updating: {str(e)}'}, status=500)
            messages.error(request, f'Error updating: {str(e)}')
            
    return redirect('deployment')

@login_required
def api_get_vehicle_allocation(request, alloc_id):
    alloc = get_object_or_404(DailyVehicleAllocation, id=alloc_id)
    return JsonResponse({
        'status': 'success',
        'allocation': {
            'id': alloc.id,
            'date': alloc.date.strftime('%Y-%m-%d'),
            'shift': alloc.shift,
            'category': alloc.category,
            'vehicle_regn': alloc.vehicle_regn,
            'driver_name': alloc.driver_name,
            'driver_emp_id': alloc.driver_emp_id or (alloc.driver.emp_id if alloc.driver else ''),
            'driver_contact': alloc.driver_contact or (alloc.driver.contact_info if alloc.driver else ''),
            'location_zone': alloc.location_zone,
            'work_order_no': alloc.work_order_no or '',
            'vendor': alloc.vendor or '',
            'out_time': alloc.out_time or '',
            'in_time': alloc.in_time or '',
            'remarks': alloc.remarks or '',
        }
    })

@login_required
def api_delete_vehicle_allocation(request, alloc_id):
    try:
        alloc = DailyVehicleAllocation.objects.get(id=alloc_id)
        d_val = alloc.date
        cat = alloc.category
        v_regn = alloc.vehicle_regn
        alloc.delete()
        _sync_allocation_to_deployment(d_val, cat)
        messages.success(request, 'Allocation removed successfully.')
        log_activity(request.user, 'DELETE', 'Vehicle Allocation', f"Removed allocation #{alloc_id} for {v_regn} ({cat})", request)
    except DailyVehicleAllocation.DoesNotExist:
        messages.error(request, 'Record not found.')
    return redirect('deployment')

@login_required
def api_deployment_vehicle_lookup(request):
    regn = (request.GET.get('regn') or '').strip()
    from fleet.models import FleetVehicle, HiredVehicle
    from .models import Employee, DailyVehicleAllocation
    import re
    
    def _clean_r(s):
        return re.sub(r'[^A-Za-z0-9]', '', str(s or '')).upper()
        
    clean_r = _clean_r(regn)
    if not clean_r:
        return JsonResponse({'found': False})
        
    all_v = list(FleetVehicle.objects.all())
    matched_v = [v for v in all_v if _clean_r(v.regn) == clean_r or _clean_r(v.dno) == clean_r]
    
    all_h = list(HiredVehicle.objects.all())
    matched_h = [h for h in all_h if _clean_r(h.regn) == clean_r]
    
    v = matched_v[0] if matched_v else None
    hired = matched_h[0] if matched_h else None

    # Check previous DailyVehicleAllocation as well
    last_alloc = None
    for a in DailyVehicleAllocation.objects.all().order_by('-id'):
        if _clean_r(a.vehicle_regn) == clean_r:
            last_alloc = a
            break
    
    if v or hired or last_alloc:
        v_extra = v.extra_data if (v and isinstance(v.extra_data, dict)) else {}
        stored_cat = v_extra.get('category') or (last_alloc.category if last_alloc else '')
        model_name = (v.model_name if v else '') or (hired.equipment_type if hired else '') or (last_alloc.category if last_alloc else '')
        cat = stored_cat or _get_machinery_category(model_name)
        
        driver_name = ''
        driver_emp_id = ''
        driver_phone = ''
        if v and v.driver:
            driver_name = v.driver.name
            driver_phone = v.driver.phone or ''
            
        if v:
            emp = Employee.objects.filter(assigned_vehicle=v).first()
            if emp:
                driver_name = emp.name
                driver_emp_id = emp.emp_id or ''
                driver_phone = emp.contact_info or ''

        if not driver_name and last_alloc:
            driver_name = last_alloc.driver_name or ''
            driver_emp_id = last_alloc.driver_emp_id or ''
            driver_phone = last_alloc.driver_contact or ''
        
        # WO NO and Vendor from HiredVehicle, FleetVehicle.extra_data or last_alloc
        wo_no = (hired.agreement_ref if hired else '') or v_extra.get('contract_ref', '') or (last_alloc.work_order_no if last_alloc else '') or ''
        vendor_name = (hired.owner_name if hired else '') or v_extra.get('owner_name', '') or (last_alloc.vendor if last_alloc else '') or 'In-House (RIGSAR-VAJRA)'
                
        return JsonResponse({
            'found': True,
            'id': v.id if v else 0,
            'regn': (v.regn if v else (hired.regn if hired else (last_alloc.vehicle_regn if last_alloc else regn))),
            'model_name': model_name,
            'category': cat or 'Scania',
            'work_order_no': wo_no,
            'vendor': vendor_name,
            'driver_name': driver_name,
            'driver_emp_id': driver_emp_id,
            'driver_phone': driver_phone or '',
        })
        
    return JsonResponse({'found': False})

@login_required
def add_deployment_action(request):
    if request.method == 'POST':
        try:
            d = request.POST.get('date')
            m = request.POST.get('machinery')
            
            if not d or not m:
                messages.error(request, 'Date and Machinery are required.')
                return redirect('deployment')
                
            dep, created = DailyDeployment.objects.get_or_create(date=d, machinery=m)
            
            dep.zone_1_2_day = int(request.POST.get('z12_d') or 0)
            dep.zone_1_2_night = int(request.POST.get('z12_n') or 0)
            dep.zone_3_4_day = int(request.POST.get('z34_d') or 0)
            dep.zone_3_4_night = int(request.POST.get('z34_n') or 0)
            dep.borrow_area_day = int(request.POST.get('ba_d') or 0)
            dep.borrow_area_night = int(request.POST.get('ba_n') or 0)
            dep.culvert_area_day = int(request.POST.get('ca_d') or 0)
            dep.culvert_area_night = int(request.POST.get('ca_n') or 0)
            dep.batching_plant_day = int(request.POST.get('bp_d') or 0)
            dep.batching_plant_night = int(request.POST.get('bp_n') or 0)
            dep.crushing_plant_day = int(request.POST.get('cp_d') or 0)
            dep.crushing_plant_night = int(request.POST.get('cp_n') or 0)
            dep.road_maint_day = int(request.POST.get('rm_d') or 0)
            dep.road_maint_night = int(request.POST.get('rm_n') or 0)
            dep.entered_by = request.user
            
            dep.save()
            messages.success(request, 'Deployment record saved successfully.')
        except Exception as e:
            messages.error(request, f'Error saving record: {str(e)}')
            
    return redirect('deployment')

@login_required
@csrf_exempt
def import_deployment_excel(request):
    if request.method == 'POST' and request.FILES.get('excel_file'):
        try:
            import pandas as pd
            import re
            import re
            excel_file = request.FILES['excel_file']
            
            ZONE_MAP = {
                'zone 1': ('zone_1_2_day', 'zone_1_2_night'),
                'zone 3': ('zone_3_4_day', 'zone_3_4_night'),
                'borrow': ('borrow_area_day', 'borrow_area_night'),
                'culvert': ('culvert_area_day', 'culvert_area_night'),
                'batching': ('batching_plant_day', 'batching_plant_night'),
                'crushing': ('crushing_plant_day', 'crushing_plant_night'),
                'road': ('road_maint_day', 'road_maint_night'),
            }
            
            def find_date_in_sheet(df):
                for r in range(min(5, len(df))):
                    for c in range(len(df.columns)):
                        cell = str(df.iloc[r, c])
                        match = re.search(r'Date[:\s]*([0-9./\-]+)', cell, re.IGNORECASE)
                        if match:
                            date_str = match.group(1)
                            for fmt in ['%d.%m.%Y', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%y', '%d/%m/%y', '%d-%m-%y']:
                                try:
                                    return pd.to_datetime(date_str, format=fmt).date()
                                except:
                                    continue
                return None
            
            def find_header_rows(df):
                zone_row = None
                shift_row = None
                for r in range(min(10, len(df))):
                    row_text = ' '.join([str(df.iloc[r, c]).lower() for c in range(len(df.columns))])
                    if 'zone' in row_text or 'borrow' in row_text:
                        zone_row = r
                    if 'day' in row_text and ('night' in row_text or row_text.count('day') >= 3):
                        shift_row = r
                return zone_row, shift_row
            
            def build_column_map(df, zone_row, shift_row):
                col_map = {}
                current_zone_key = None
                for c in range(2, len(df.columns)):
                    zone_cell = str(df.iloc[zone_row, c]).lower().strip() if zone_row is not None else ''
                    if zone_cell and zone_cell != 'nan' and zone_cell != 'total':
                        current_zone_key = None
                        for key in ZONE_MAP:
                            if key in zone_cell:
                                current_zone_key = key
                                break
                    if current_zone_key is None:
                        continue
                    if shift_row is not None:
                        shift_cell = str(df.iloc[shift_row, c]).lower().strip()
                    else:
                        shift_cell = 'day'
                    day_field, night_field = ZONE_MAP[current_zone_key]
                    if 'night' in shift_cell:
                        col_map[c] = night_field
                    else:
                        col_map[c] = day_field
                return col_map
            
            def find_data_start(df):
                for r in range(min(15, len(df))):
                    cell_b = str(df.iloc[r, 1]).strip().lower()
                    if cell_b in ['nan', 'none', '', 'equipment description', 'equipment']:
                        continue
                    cell_a = str(df.iloc[r, 0]).strip()
                    try:
                        int(float(cell_a))
                        return r
                    except:
                        if cell_b not in ['day', 'night', 'sl. no', 'sl.no', 'total']:
                            return r
                return 3
            
            excel_data = pd.read_excel(excel_file, sheet_name=None, header=None)
            records_saved = 0
            
            for sheet_name, df in excel_data.items():
                try:
                    real_date = find_date_in_sheet(df)
                    if real_date is None:
                        for fmt in ['%d-%m-%y', '%d-%m-%Y', '%d.%m.%y', '%d.%m.%Y']:
                            try:
                                real_date = pd.to_datetime(sheet_name, format=fmt).date()
                                break
                            except:
                                continue
                    if real_date is None:
                        continue
                    
                    zone_row, shift_row = find_header_rows(df)
                    if zone_row is None:
                        continue
                    
                    col_map = build_column_map(df, zone_row, shift_row)
                    if not col_map:
                        continue
                    
                    data_start = find_data_start(df)
                    
                    for r in range(data_start, len(df)):
                        machinery = str(df.iloc[r, 1]).strip()
                        if machinery.lower() in ['nan', 'none', '', 'total', 'grand total']:
                            continue
                        
                        dep, created = DailyDeployment.objects.get_or_create(date=real_date, machinery=machinery)
                        
                        for col_idx, field_name in col_map.items():
                            try:
                                val = str(df.iloc[r, col_idx]).strip()
                                val = int(float(val)) if val and val.lower() != 'nan' else 0
                            except:
                                val = 0
                            setattr(dep, field_name, val)
                        
                        dep.entered_by = request.user
                        dep.save()
                        records_saved += 1
                        
                except Exception as e:
                    print(f"Error processing sheet {sheet_name}: {e}")
                    continue
                    
            return JsonResponse({'status': 'success', 'message': f'Processed {records_saved} records.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
            
    return JsonResponse({'status': 'error', 'message': 'Invalid request'})


@login_required
def delete_deployment_by_date_action(request):
    if request.method == 'POST':
        del_date = request.POST.get('delete_date')
        if del_date:
            try:
                deleted, _ = DailyDeployment.objects.filter(date=del_date).delete()
                messages.success(request, f'Deleted {deleted} deployment records for date {del_date}.')
            except Exception as e:
                messages.error(request, f'Error deleting records: {str(e)}')
        else:
            messages.error(request, 'Please provide a valid date to delete.')
    return redirect('deployment')

@login_required
def edit_deployment_action(request, dep_id):
    from django.shortcuts import get_object_or_404
    dep = get_object_or_404(DailyDeployment, id=dep_id)
    if request.method == 'POST':
        try:
            dep.date = request.POST.get('date')
            dep.machinery = request.POST.get('machinery')
            dep.zone_1_2_day = int(request.POST.get('z12_d') or 0)
            dep.zone_1_2_night = int(request.POST.get('z12_n') or 0)
            dep.zone_3_4_day = int(request.POST.get('z34_d') or 0)
            dep.zone_3_4_night = int(request.POST.get('z34_n') or 0)
            dep.borrow_area_day = int(request.POST.get('ba_d') or 0)
            dep.borrow_area_night = int(request.POST.get('ba_n') or 0)
            dep.culvert_area_day = int(request.POST.get('ca_d') or 0)
            dep.culvert_area_night = int(request.POST.get('ca_n') or 0)
            dep.batching_plant_day = int(request.POST.get('bp_d') or 0)
            dep.batching_plant_night = int(request.POST.get('bp_n') or 0)
            dep.crushing_plant_day = int(request.POST.get('cp_d') or 0)
            dep.crushing_plant_night = int(request.POST.get('cp_n') or 0)
            dep.road_maint_day = int(request.POST.get('rm_d') or 0)
            dep.road_maint_night = int(request.POST.get('rm_n') or 0)
            dep.entered_by = request.user
            dep.save()
            messages.success(request, 'Deployment record updated successfully.')
        except Exception as e:
            messages.error(request, f'Error updating record: {str(e)}')
    return redirect('deployment')

@login_required
def delete_deployment_action(request, dep_id):
    from django.shortcuts import get_object_or_404
    dep = get_object_or_404(DailyDeployment, id=dep_id)
    try:
        dep.delete()
        messages.success(request, 'Deployment record deleted successfully.')
    except Exception as e:
        messages.error(request, f'Error deleting record: {str(e)}')
    return redirect('deployment')

@login_required
def shifts_view(request):
    if request.user.system_role != 'MANAGER' and not request.user.is_superuser:
        if getattr(request.user, 'assigned_modules', None) and 'shift_management' not in request.user.assigned_modules:
            return redirect('dashboard')
    employees = Employee.objects.select_related('entered_by', 'assigned_vehicle').all().order_by('name')
    # Filter by search
    search_q = request.GET.get('q', '')
    if search_q:
        employees = employees.filter(name__icontains=search_q) | employees.filter(emp_id__icontains=search_q)
    
    designations = Employee.objects.exclude(designation__isnull=True).exclude(designation='').values_list('designation', flat=True).distinct().order_by('designation')
    
    total_emps = Employee.objects.count()
    latest_emp = Employee.objects.order_by('-id').first()
    latest_emp_id = latest_emp.id if latest_emp else 0
    vehicles = FleetVehicle.objects.filter(is_active=True).order_by('regn', 'dno')

    return render(request, 'shifts.html', {
        'employees': employees,
        'search_q': search_q,
        'designations': designations,
        'total_emps': total_emps,
        'latest_emp_id': latest_emp_id,
        'vehicles': vehicles,
    })

@login_required
def update_shift_action(request):
    if request.method == 'POST':
        emp_id = request.POST.get('employee_id')
        shift = request.POST.get('shift')
        remarks = request.POST.get('remarks')
        vehicle_id = request.POST.get('vehicle_id')
        if emp_id:
            try:
                emp = Employee.objects.get(id=emp_id)
                emp.current_shift = shift
                emp.shift_remarks = remarks
                if vehicle_id:
                    emp.assigned_vehicle_id = vehicle_id
                else:
                    emp.assigned_vehicle = None
                emp.entered_by = request.user
                emp.save()
                messages.success(request, f"Shift and Vehicle assignment updated for {emp.name}.")
            except Exception as e:
                messages.error(request, f"Error: {str(e)}")
    return redirect('shifts')


@login_required
def import_employees_excel(request):
    if request.method == 'POST' and request.FILES.get('excel_file'):
        try:
            import pandas as pd
            excel_file = request.FILES['excel_file']
            
            # Use openpyxl to get visible sheets only
            import openpyxl
            wb = openpyxl.load_workbook(excel_file, data_only=True, read_only=True)
            visible_sheets = [s.title for s in wb.worksheets if s.sheet_state == 'visible']
            
            df = pd.DataFrame()
            if visible_sheets:
                df = pd.read_excel(excel_file, sheet_name=visible_sheets[0])
            else:
                df = pd.read_excel(excel_file)
                
            count = 0
            
            # Map columns flexibly
            col_map = {}
            for col in df.columns:
                c = str(col).lower()
                if 'id' in c or 'reg' in c: col_map['emp_id'] = col
                elif 'name' in c: col_map['name'] = col
                elif 'national' in c: col_map['nationality'] = col
                elif 'desig' in c: col_map['designation'] = col
                elif 'dept' in c or 'department' in c: col_map['department'] = col
                elif 'join' in c or 'date' in c: col_map['joining_date'] = col
                elif 'permit no' in c or 'work permit' in c: col_map['work_permit_no'] = col
                elif 'permit exp' in c or 'expiry' in c: col_map['work_permit_expiry'] = col
                elif 'passport' in c: col_map['passport_details'] = col
                elif 'contact' in c or 'phone' in c: col_map['contact_info'] = col
                elif 'status' in c: col_map['status'] = col
                elif 'agency' in c or 'contractor' in c: col_map['contractor_agency'] = col

            for _, row in df.iterrows():
                # Extract using map or fallback to None
                emp_id = str(row.get(col_map.get('emp_id', 'EMP_ID'), '')).strip() if pd.notna(row.get(col_map.get('emp_id', ''))) else None
                name = str(row.get(col_map.get('name', 'NAME'), '')).strip() if pd.notna(row.get(col_map.get('name', ''))) else None
                
                if not name:
                    continue # Skip empty rows
                
                nationality = str(row.get(col_map.get('nationality', 'NATIONALITY'), 'Bhutanese')).strip() if pd.notna(row.get(col_map.get('nationality', ''))) else 'Bhutanese'
                designation = str(row.get(col_map.get('designation', 'DESIGNATION'), 'General')).strip() if pd.notna(row.get(col_map.get('designation', ''))) else 'General'
                department = str(row.get(col_map.get('department', 'DEPARTMENT'), 'P&M')).strip() if pd.notna(row.get(col_map.get('department', ''))) else 'P&M'
                
                contact = str(row.get(col_map.get('contact_info', 'CONTACT'), '')).strip() if pd.notna(row.get(col_map.get('contact_info', ''))) else ''
                agency = str(row.get(col_map.get('contractor_agency', 'AGENCY'), '')).strip() if pd.notna(row.get(col_map.get('contractor_agency', ''))) else ''
                passport = str(row.get(col_map.get('passport_details', 'PASSPORT'), '')).strip() if pd.notna(row.get(col_map.get('passport_details', ''))) else ''
                permit = str(row.get(col_map.get('work_permit_no', 'PERMIT'), '')).strip() if pd.notna(row.get(col_map.get('work_permit_no', ''))) else ''
                status = str(row.get(col_map.get('status', 'STATUS'), 'Active')).strip() if pd.notna(row.get(col_map.get('status', ''))) else 'Active'
                if status.lower() not in ['active', 'on leave', 'terminated']: status = 'Active'

                # Dates
                joining = row.get(col_map.get('joining_date', 'JOINING'))
                if pd.notna(joining):
                    try: joining = pd.to_datetime(joining).date()
                    except: joining = None
                else: joining = None
                
                expiry = row.get(col_map.get('work_permit_expiry', 'EXPIRY'))
                if pd.notna(expiry):
                    try: expiry = pd.to_datetime(expiry).date()
                    except: expiry = None
                else: expiry = None

                # Update or create
                if emp_id:
                    emp, created = Employee.objects.update_or_create(
                        emp_id=emp_id,
                        defaults={
                            'name': name,
                            'nationality': nationality,
                            'designation': designation,
                            'department': department,
                            'contact_info': contact,
                            'contractor_agency': agency,
                            'passport_details': passport,
                            'work_permit_no': permit,
                            'status': status.title(),
                            'joining_date': joining,
                            'work_permit_expiry': expiry,
                            'entered_by': request.user
                        }
                    )
                else:
                    emp, created = Employee.objects.get_or_create(
                        name=name,
                        designation=designation,
                        department=department,
                        defaults={
                            'nationality': nationality,
                            'contact_info': contact,
                            'contractor_agency': agency,
                            'passport_details': passport,
                            'work_permit_no': permit,
                            'status': status.title(),
                            'joining_date': joining,
                            'work_permit_expiry': expiry,
                            'entered_by': request.user
                        }
                    )
                
                count += 1
                
            messages.success(request, f"Successfully imported {count} employees from Excel.")
        except Exception as e:
            messages.error(request, f"Error processing Excel file: {str(e)}")
            
    return redirect('dashboard')


@login_required
def download_employee_template(request):
    import pandas as pd
    from django.http import HttpResponse
    import io

    df = pd.DataFrame(columns=[
        'EMP_ID', 'NAME', 'NATIONALITY', 'DESIGNATION', 'DEPARTMENT', 
        'JOINING_DATE', 'WORK_PERMIT', 'WORK_PERMIT_EXPIRY', 
        'PASSPORT', 'CONTACT', 'STATUS', 'AGENCY'
    ])
    
    # Add a sample row
    df.loc[0] = [
        'EMP-001', 'John Doe', 'Bhutanese', 'Operator', 'P&M', 
        '2026-01-15', 'WP-12345', '2027-01-14', 
        'P-98765', '9876543210', 'Active', 'Direct'
    ]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Employees')
    
    output.seek(0)
    response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Employee_Upload_Template.xlsx'
    return response


@login_required
def download_shift_template(request):
    import pandas as pd
    from django.http import HttpResponse
    import io

    df = pd.DataFrame(columns=['EMP_ID', 'NAME', 'CURRENT_SHIFT', 'ROLE_REMARKS'])
    
    # Add sample
    df.loc[0] = ['EMP-001', 'John Doe', 'Night', 'Helps in issuing parts']
    df.loc[1] = ['EMP-002', 'Jane Smith', 'Day', '']

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Shifts')
    
    output.seek(0)
    response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Shift_Upload_Template.xlsx'
    return response

@login_required
def import_shifts_excel(request):
    if request.method == 'POST' and request.FILES.get('excel_file'):
        try:
            import pandas as pd
            import openpyxl
            excel_file = request.FILES['excel_file']
            
            wb = openpyxl.load_workbook(excel_file, data_only=True, read_only=True)
            visible_sheets = [s.title for s in wb.worksheets if s.sheet_state == 'visible']
            
            df = pd.DataFrame()
            if visible_sheets:
                df = pd.read_excel(excel_file, sheet_name=visible_sheets[0])
            else:
                df = pd.read_excel(excel_file)
                
            count = 0
            
            # Find header row
            header_idx = 0
            for i, r in df.iterrows():
                row_str = ' '.join([str(v).lower() for v in r.values])
                if 'id' in row_str or 'name' in row_str or 'emp' in row_str:
                    header_idx = i
                    break
            
            if header_idx > 0:
                df.columns = df.iloc[header_idx]
                df = df.iloc[header_idx + 1:]
                
            df.columns = [str(c).strip() for c in df.columns]
            
            for col in df.columns:
                c = str(col).lower()
                if 'id' in c or 'reg' in c or 'employment' in c: df = df.rename(columns={col: 'EMP_ID'})
                elif 'shift' in c: df = df.rename(columns={col: 'CURRENT_SHIFT'})
                elif 'role' in c or 'remark' in c: df = df.rename(columns={col: 'ROLE_REMARKS'})
                
            for _, row in df.iterrows():
                emp_id = str(row.get('EMP_ID', '')).strip()
                if not emp_id or emp_id.lower() == 'nan':
                    continue
                    
                shift = str(row.get('CURRENT_SHIFT', '')).strip()
                remarks = str(row.get('ROLE_REMARKS', '')).strip()
                if remarks.lower() == 'nan': remarks = ''
                
                # If no explicit shift column, try to parse from roles/remarks
                if not shift or shift.lower() == 'nan':
                    if 'day' in remarks.lower():
                        shift = 'Day'
                    elif 'night' in remarks.lower():
                        shift = 'Night'
                    else:
                        shift = 'Day'
                else:
                    shift = shift.title()
                    if 'Day' in shift: shift = 'Day'
                    elif 'Night' in shift: shift = 'Night'
                    else: shift = 'Day'
                
                try:
                    emp = Employee.objects.get(emp_id=emp_id)
                    emp.current_shift = shift
                    if remarks:
                        emp.shift_remarks = remarks
                    emp.entered_by = request.user
                    emp.save()
                    count += 1
                except Employee.DoesNotExist:
                    pass
            
            messages.success(request, f"Successfully updated shifts for {count} employees.")
        except Exception as e:
            messages.error(request, f"Error processing Excel file: {str(e)}")
            
    return redirect('shifts')


def export_shifts_report(request):
    import pandas as pd
    import json
    from django.http import HttpResponse, JsonResponse
    import io
    import datetime

    if request.method == "POST":
        try:
            data = json.loads(request.body)
            format_type = data.get('format', 'excel')
            report_title = data.get('report_title', 'Shifts Report')
            status_filter = data.get('status', 'all')
            shift_filter = data.get('shift', 'all')
            designation_filter = data.get('designation', '')
            date_from = data.get('date_from', '')
            date_to = data.get('date_to', '')
            columns = data.get('columns', [])
            
            employees = Employee.objects.select_related('entered_by').all().order_by('name')
            if status_filter != 'all':
                employees = employees.filter(status=status_filter)
            if shift_filter != 'all':
                employees = employees.filter(current_shift=shift_filter)
            if designation_filter:
                employees = employees.filter(designation__icontains=designation_filter)
            if date_from:
                employees = employees.filter(created_at__date__gte=date_from)
            if date_to:
                employees = employees.filter(created_at__date__lte=date_to)
                
            report_data = []
            for i, emp in enumerate(employees, 1):
                row = {}
                if "SL.NO" in columns: row["SL.NO"] = i
                if "NAME" in columns: row["NAME"] = emp.name
                if "EMPLOYMENT ID" in columns: row["EMPLOYMENT ID"] = emp.emp_id
                if "DESIGNATION" in columns: row["DESIGNATION"] = emp.designation
                if "CURRENT SHIFT" in columns: row["CURRENT SHIFT"] = emp.current_shift
                if "ROLE / REMARKS" in columns: row["ROLE / REMARKS"] = emp.shift_remarks
                report_data.append(row)
                
            df = pd.DataFrame(report_data)
            
            if format_type == 'excel':
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Shifts')
                output.seek(0)
                response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                date_str = datetime.datetime.now().strftime("%Y-%m-%d")
                response['Content-Disposition'] = f'attachment; filename=Shifts_{date_str}.xlsx'
                return response
            else:
                # Generate HTML for PDF printing
                html = f"""
                <html>
                <head>
                    <title>{report_title}</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; margin: 40px; }}
                        h2 {{ text-align: center; color: #333; }}
                        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                        th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; font-size:12px; }}
                        th {{ background-color: #f8fafc; font-weight:bold; }}
                        .footer {{ margin-top: 30px; font-size:10px; text-align:center; color:#666; }}
                    </style>
                </head>
                <body onload="window.print()">
                    <h2>{report_title}</h2>
                    <p style="text-align:center; font-size:12px; color:#555;">Generated on: {datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")}</p>
                    <table>
                        <thead>
                            <tr>"""
                if not report_data:
                    html += "<th>No data found</th></tr></thead><tbody></tbody></table>"
                else:
                    for col in report_data[0].keys():
                        html += f"<th>{col}</th>"
                    html += "</tr></thead><tbody>"
                    for row in report_data:
                        html += "<tr>"
                        for val in row.values():
                            html += f"<td>{val or '-'}</td>"
                        html += "</tr>"
                    html += "</tbody></table>"
                    
                html += """
                    <div class="footer">Confidential - For internal use only.</div>
                </body>
                </html>
                """
                return HttpResponse(html, content_type='text/html')
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    else:
        # Fallback for GET request if still accessed directly
        return JsonResponse({'error': 'Please use the Advanced Report modal to export data.'}, status=400)

@login_required
def bulk_update_shift_api(request):
    import json
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            emp_ids = data.get('employee_ids', [])
            shift = data.get('shift')
            if not emp_ids or not shift:
                return JsonResponse({'status': 'error', 'message': 'Missing data'}, status=400)

            # Update individually so entered_by is tracked per employee
            updated = 0
            for emp in Employee.objects.filter(id__in=emp_ids):
                emp.current_shift = shift
                emp.entered_by = request.user
                emp.save(update_fields=['current_shift', 'entered_by'])
                updated += 1

            entered_name = request.user.full_name or request.user.username
            return JsonResponse({'status': 'success', 'updated': updated, 'entered_by': entered_name})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)

def download_deployment_template(request):
    import io
    import pandas as pd
    from django.http import HttpResponse
    from django.utils import timezone
    
    # Create an in-memory output file for the new workbook.
    output = io.BytesIO()
    
    today_str = timezone.now().strftime('%d-%m-%Y')
    
    # Define columns
    # We will use MultiIndex for header to match what pandas can easily write, or just write it row by row
    # To have complex headers, using pandas ExcelWriter and XlsxWriter engine is better.
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book
        worksheet = workbook.add_worksheet(today_str)
        
        # Formatting
        header_format = workbook.add_format({
            'bold': True, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'bg_color': '#D9D9D9'
        })
        
        date_format = workbook.add_format({'bold': True, 'align': 'left'})
        worksheet.write(0, 0, f'Date: {today_str}', date_format)
        
        # Row 1: Zones
        zones = [
            ('Zone 1', 2), ('Zone 3', 4), ('Borrow Area', 6), 
            ('Culvert', 8), ('Batching Plant', 10), ('Crushing Plant', 12), ('Road Maint', 14)
        ]
        
        # Write headers
        worksheet.write(2, 0, 'Sl. No', header_format)
        worksheet.write(2, 1, 'Machinery', header_format)
        worksheet.write(3, 0, '', header_format)
        worksheet.write(3, 1, '', header_format)
        
        for name, col in zones:
            worksheet.merge_range(2, col, 2, col+1, name, header_format)
            worksheet.write(3, col, 'Day', header_format)
            worksheet.write(3, col+1, 'Night', header_format)
            
        # Add some sample machinery
        machines = ['Excavator 1', 'Excavator 2', 'Tipper 1', 'Tipper 2', 'Roller 1', 'Grader 1']
        for i, m in enumerate(machines):
            worksheet.write(4+i, 0, i+1)
            worksheet.write(4+i, 1, m)
            for col in range(2, 16):
                worksheet.write(4+i, col, 0)
                
        worksheet.set_column(1, 1, 20)
        
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename=Deployment_Sample_{today_str}.xlsx'
    return response



@login_required
def api_live_dashboard(request):
    """Return live counts for dashboard cards."""
    from portal.models import DailyDeployment, Employee, EmployeeDocument, VehicleDocument, InsuranceDocument
    from fleet.models import LubricationLog, FleetVehicle
    from django.db.models import Sum
    from django.utils.timezone import now
    today_date = now().date()
    
    lubricants_cost = LubricationLog.objects.aggregate(t=Sum('total_amount'))['t'] or 0
    expired_docs_count = (
        EmployeeDocument.objects.filter(expiry_date__lte=today_date).count() +
        VehicleDocument.objects.filter(rc_expiry_date__lte=today_date).count() +
        InsuranceDocument.objects.filter(expiry_date__lte=today_date).count()
    )

    import datetime
    from fleet.models import VehicleMovement, TyreLog, HiredVehicle, RepairLog

    today = today_date
    month_start = today.replace(day=1)
    soon = today + timedelta(days=30)

    # Employee breakdowns
    emp_active  = Employee.objects.filter(status='Active').count()
    emp_leave   = Employee.objects.filter(status='On Leave').count()
    emp_term    = Employee.objects.filter(status='Terminated').count()
    foreign_active  = Employee.objects.exclude(nationality__icontains='bhutan').filter(status='Active').count()
    national_active = Employee.objects.filter(nationality__icontains='bhutan', status='Active').count()

    # Vehicle breakdown
    total_v      = FleetVehicle.objects.count()
    workshop_ids = RepairLog.objects.filter(out_date__isnull=True, vehicle__isnull=False).values_list('vehicle_id', flat=True).distinct()
    veh_workshop = len(set(workshop_ids))
    veh_running  = max(total_v - veh_workshop, 0)
    veh_hired    = HiredVehicle.objects.count()

    # Vehicle movements
    mv_qs           = VehicleMovement.objects.all()
    movements_total = mv_qs.count()
    movements_today = mv_qs.filter(movement_date=today).count()
    last_mv         = mv_qs.order_by('-movement_date', '-id').first()
    movements_last  = f"{last_mv.vehicle_number} → {last_mv.destination or '-'}" if last_mv else ''

    # Tyre
    tq              = TyreLog.objects.filter(date__gte=month_start)
    tyre_month_count= tq.count()
    tyre_month_cost = float(tq.aggregate(s=Sum('total_amount'))['s'] or 0)

    # Lubricant entries this month
    lq              = LubricationLog.objects.filter(date__gte=month_start)
    lub_month_count = lq.count()
    
    # Spare Parts
    from fleet.models import SparePart, SparePartTransaction
    from django.db.models import F
    spare_parts_val = float(SparePartTransaction.objects.aggregate(t=Sum('total_amount'))['t'] or 0)
    sp_low_stock = SparePart.objects.filter(current_stock__lte=F('reorder_level')).count()
    sp_month_in = SparePartTransaction.objects.filter(date__gte=month_start, transaction_type='IN').aggregate(s=Sum('quantity'))['s'] or 0
    sp_month_out = SparePartTransaction.objects.filter(date__gte=month_start, transaction_type='OUT').aggregate(s=Sum('quantity'))['s'] or 0
    

    # Documents
    dep_today       = DailyDeployment.objects.filter(date=today).count()
    latest_dep      = DailyDeployment.objects.order_by('-date', '-id').first()
    dep_latest_date = latest_dep.date.strftime('%d %b %Y') if latest_dep else ''

    docs_expiring_soon = (
        EmployeeDocument.objects.filter(expiry_date__gt=today, expiry_date__lte=soon).count() +
        VehicleDocument.objects.filter(rc_expiry_date__gt=today, rc_expiry_date__lte=soon).count() +
        InsuranceDocument.objects.filter(expiry_date__gt=today, expiry_date__lte=soon).count()
    )

    return JsonResponse({
        # Main counters (existing)
        'deployments':      DailyDeployment.objects.count(),
        'employees':        Employee.objects.count(),
        'national':         Employee.objects.filter(nationality__icontains='bhutan').count(),
        'foreign':          Employee.objects.exclude(nationality__icontains='bhutan').count(),
        'vehicles':         total_v,
        'lubricants_cost':  round(float(lubricants_cost), 2),
        'expired_docs':     expired_docs_count,
        'shift_day':        Employee.objects.filter(current_shift='Day').count(),
        'shift_night':      Employee.objects.filter(current_shift='Night').count(),
        'vehicle_movements':movements_total,
        'tyre_count':       tyre_month_count,
        'spare_parts_val':  spare_parts_val,
        # Preview rows
        'previews': {
            'dep_today':          dep_today,
            'dep_latest_date':    dep_latest_date,
            'emp_active':         emp_active,
            'emp_leave':          emp_leave,
            'emp_term':           emp_term,
            'foreign_active':     foreign_active,
            'national_active':    national_active,
            'veh_running':        veh_running,
            'veh_workshop':       veh_workshop,
            'veh_hired':          veh_hired,
            'movements_total':    movements_total,
            'movements_today':    movements_today,
            'movements_last':     movements_last,
            'tyre_month_count':   tyre_month_count,
            'tyre_month_cost':    round(tyre_month_cost, 0),
            'lub_month_count':    lub_month_count,
            'docs_expiring_soon': docs_expiring_soon,
            'sp_low_stock':       sp_low_stock,
            'sp_month_in':        sp_month_in,
            'sp_month_out':       sp_month_out,
        }
    })

@login_required
def api_live_deployments(request):
    """Return deployments updated or created after ?since_ts=UNIX_TIMESTAMP."""
    from portal.models import DailyDeployment
    import time
    from django.utils import timezone
    import datetime

    since_ts = float(request.GET.get('since_ts', 0))
    since_dt = datetime.datetime.fromtimestamp(since_ts, tz=datetime.timezone.utc) if since_ts else None

    # Get all active IDs currently in the database to detect deletions
    all_ids = list(DailyDeployment.objects.values_list('id', flat=True))

    if since_dt:
        deps = DailyDeployment.objects.select_related('entered_by').filter(updated_at__gte=since_dt)
    else:
        deps = DailyDeployment.objects.select_related('entered_by').all().order_by('-date')[:100]

    data = []
    for dep in deps:
        data.append({
            'id': dep.id,
            'date': dep.date.strftime('%Y-%m-%d') if dep.date else '',
            'date_formatted': dep.date.strftime('%d M %Y') if dep.date else '',
            'machinery': dep.machinery or '',
            'zone_1_2_day': dep.zone_1_2_day,
            'zone_1_2_night': dep.zone_1_2_night,
            'zone_3_4_day': dep.zone_3_4_day,
            'zone_3_4_night': dep.zone_3_4_night,
            'borrow_area_day': dep.borrow_area_day,
            'borrow_area_night': dep.borrow_area_night,
            'culvert_area_day': dep.culvert_area_day,
            'culvert_area_night': dep.culvert_area_night,
            'batching_plant_day': dep.batching_plant_day,
            'batching_plant_night': dep.batching_plant_night,
            'crushing_plant_day': dep.crushing_plant_day,
            'crushing_plant_night': dep.crushing_plant_night,
            'road_maint_day': dep.road_maint_day,
            'road_maint_night': dep.road_maint_night,
            'total_day': dep.total_day,
            'total_night': dep.total_night,
            'entered_by': (dep.entered_by.full_name or dep.entered_by.username) if dep.entered_by else 'System',
        })

    return JsonResponse({
        'rows': data,
        'all_ids': all_ids,
        'count': len(all_ids),
        'server_ts': time.time()
    })

@login_required
def api_live_shifts(request):
    """Return employee shift data updated after ?since_ts=UNIX_TIMESTAMP."""
    from portal.models import Employee
    import time
    from django.utils import timezone
    import datetime

    since_ts = float(request.GET.get('since_ts', 0))
    since_dt = datetime.datetime.fromtimestamp(since_ts, tz=datetime.timezone.utc) if since_ts else None

    if since_dt:
        # Return employees whose shift was updated since the given timestamp
        emps = Employee.objects.select_related('entered_by').filter(created_at__gte=since_dt) | \
               Employee.objects.select_related('entered_by').filter(entered_by__isnull=False)
        # Actually just return ALL employees so client can compare and update any changed rows
        emps = Employee.objects.select_related('entered_by').all()
    else:
        emps = Employee.objects.select_related('entered_by').all()

    data = []
    for emp in emps:
        data.append({
            'id': emp.id,
            'emp_id': emp.emp_id or '',
            'name': emp.name or '',
            'designation': emp.designation or '',
            'current_shift': emp.current_shift or 'Day',
            'shift_remarks': emp.shift_remarks or '',
            'status': emp.status or 'Active',
            'entered_by': (emp.entered_by.full_name or emp.entered_by.username) if emp.entered_by else 'System',
        })
    total = Employee.objects.count()
    return JsonResponse({'rows': data, 'count': total, 'server_ts': time.time()})

@login_required
def api_global_poll(request):
    """
    Lightweight JSON endpoint to poll for changes in all major models:
    DailyDeployment, Employee, LubricationLog
    """
    from django.http import JsonResponse
    from portal.models import DailyDeployment, Employee
    from fleet.models import LubricationLog

    latest_dep = DailyDeployment.objects.order_by('-id').first()
    latest_emp = Employee.objects.order_by('-id').first()
    latest_lub = LubricationLog.objects.order_by('-id').first()
    
    from fleet.models import SparePartTransaction, TyreLog, VehicleMovement
    latest_spare = SparePartTransaction.objects.order_by('-id').first()
    latest_tyre = TyreLog.objects.order_by('-id').first()
    latest_mv = VehicleMovement.objects.order_by('-id').first()

    return JsonResponse({
        'deployments_count': DailyDeployment.objects.count(),
        'deployments_latest_id': latest_dep.id if latest_dep else 0,
        'employees_count': Employee.objects.count(),
        'employees_latest_id': latest_emp.id if latest_emp else 0,
        'lubricants_count': LubricationLog.objects.count(),
        'lubricants_latest_id': latest_lub.id if latest_lub else 0,
        'spares_count': SparePartTransaction.objects.count(),
        'spares_latest_id': latest_spare.id if latest_spare else 0,
        'tyres_count': TyreLog.objects.count(),
        'tyres_latest_id': latest_tyre.id if latest_tyre else 0,
        'movements_count': VehicleMovement.objects.count(),
        'movements_latest_id': latest_mv.id if latest_mv else 0,
    })

@login_required
def chat_room(request):
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    can_chat = (
        request.user.is_superuser or
        request.user.system_role in ['MANAGER', 'PROJECT_MANAGER', 'DEO'] or
        'chat' in allowed_modules
    )
    if not can_chat:
        messages.error(request, "Permission Denied: Team Chat access is not enabled for your account.")
        return redirect('dashboard')

    # Render the chat interface with list of active users.
    from django.contrib.auth import get_user_model
    User = get_user_model()
    # Fetch all active users except the current user
    chat_users = User.objects.exclude(id=request.user.id).exclude(system_role='PENDING').order_by('full_name', 'username')
    # Users currently online (heartbeat within last 60s) for initial render
    online_ids = [u.id for u in chat_users if u.is_online()]
    return render(request, 'chat.html', {
        'chat_users': chat_users,
        'online_ids': online_ids,
    })

@login_required
def api_send_message(request):
    # POST endpoint to send a chat message.
    import json
    from django.http import JsonResponse
    from django.contrib.auth import get_user_model
    from portal.models import Message
    User = get_user_model()
    
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            receiver_id = data.get('receiver_id')
            content = data.get('content', '').strip()
            
            if not receiver_id or not content:
                return JsonResponse({'status': 'error', 'message': 'Missing fields'}, status=400)
                
            receiver = User.objects.get(id=receiver_id)
            msg = Message.objects.create(sender=request.user, receiver=receiver, content=content)
            
            return JsonResponse({
                'status': 'success',
                'message': {
                    'id': msg.id,
                    'sender_id': msg.sender.id,
                    'content': msg.content,
                    'timestamp': msg.timestamp.isoformat()
                }
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)

@login_required
def api_get_messages(request, user_id):
    # GET endpoint to fetch messages between logged-in user and user_id.
    from django.db.models import Q
    from django.http import JsonResponse
    from portal.models import Message
    
    # Fetch all messages between request.user and user_id
    messages = Message.objects.filter(
        Q(sender=request.user, receiver_id=user_id) |
        Q(sender_id=user_id, receiver=request.user)
    ).order_by('timestamp')
    
    # Mark messages received by request.user from user_id as read
    messages.filter(receiver=request.user, is_read=False).update(is_read=True)
    
    data = []
    for m in messages:
        data.append({
            'id': m.id,
            'sender_id': m.sender_id,
            'content': m.content,
            'timestamp': m.timestamp.isoformat(),
            'file_type': m.file_type,
            'file_url': m.file.url if m.file else None,
        })
        
    return JsonResponse({'messages': data})

@login_required
def api_unread_chats(request):
    # GET endpoint to retrieve unread message count per sender.
    from django.db.models import Count
    from django.http import JsonResponse
    from portal.models import Message
    from django.utils.timezone import localtime
    
    unread = Message.objects.filter(receiver=request.user, is_read=False).values('sender_id').annotate(count=Count('id'))
    unread_dict = {item['sender_id']: item['count'] for item in unread}
    total_unread = sum(unread_dict.values())
    senders_count = len(unread_dict)
    
    last_msg = Message.objects.filter(receiver=request.user, is_read=False).order_by('-timestamp').first()
    last_msg_details = {}
    if last_msg:
        sender_name = last_msg.sender.full_name or last_msg.sender.username
        local_ts = localtime(last_msg.timestamp)
        ts_str = local_ts.strftime('%d-%m-%Y %I:%M %p')
        content = last_msg.content if last_msg.content else "[Audio/File]"
        if len(content) > 35:
            content = content[:35] + "..."
        last_msg_details = {
            'sender': sender_name,
            'content': content,
            'time': ts_str
        }
        
    return JsonResponse({
        'unread': unread_dict, 
        'total': total_unread,
        'senders_count': senders_count,
        'last_msg': last_msg_details
    })

@login_required
def api_chat_heartbeat(request):
    """POST endpoint the client pings periodically to mark the user online."""
    from django.utils import timezone
    from django.http import JsonResponse
    if request.method == 'POST':
        request.user.last_seen = timezone.now()
        request.user.save(update_fields=['last_seen'])
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)

@login_required
def api_chat_presence(request):
    """GET endpoint returning which chat users are currently online."""
    from django.http import JsonResponse
    from django.contrib.auth import get_user_model
    User = get_user_model()
    users = User.objects.exclude(id=request.user.id).exclude(system_role='PENDING')
    presence = {}
    for u in users:
        presence[str(u.id)] = {
            'online': u.is_online(),
            'last_seen': u.last_seen.isoformat() if u.last_seen else None,
        }
    return JsonResponse({
        'online': [uid for uid, info in presence.items() if info['online']],
        'users': presence,
    })

@login_required
def api_send_audio(request):
    """POST (multipart) endpoint to send a voice-note: fields `receiver_id` + `audio` file."""
    from django.http import JsonResponse
    from django.contrib.auth import get_user_model
    from django.core.files.base import ContentFile
    from portal.models import Message
    User = get_user_model()

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)
    try:
        receiver_id = request.POST.get('receiver_id')
        audio_file = request.FILES.get('audio')
        if not receiver_id or not audio_file:
            return JsonResponse({'status': 'error', 'message': 'Missing receiver or audio'}, status=400)

        MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB cap
        if audio_file.size > MAX_AUDIO_BYTES:
            return JsonResponse({'status': 'error', 'message': 'Audio too large (max 10 MB)'}, status=400)

        name = (audio_file.name or '').lower()
        ctype = (getattr(audio_file, 'content_type', '') or '').lower()
        allowed_ext = ('.webm', '.ogg', '.oga', '.mp3', '.m4a', '.aac', '.wav')
        if not (ctype.startswith('audio/') or name.endswith(allowed_ext)):
            return JsonResponse({'status': 'error', 'message': 'Unsupported audio format'}, status=400)

        receiver = User.objects.get(id=receiver_id)
        ext = ''
        for e in allowed_ext:
            if name.endswith(e):
                ext = e
                break
        if not ext:
            ext = '.webm'
        msg = Message.objects.create(
            sender=request.user,
            receiver=receiver,
            content='',
            file_type='audio',
        )
        msg.file.save(f"voice_{msg.id}_{request.user.id}{ext}", ContentFile(audio_file.read()), save=True)

        return JsonResponse({
            'status': 'success',
            'message': {
                'id': msg.id,
                'sender_id': msg.sender_id,
                'content': '',
                'timestamp': msg.timestamp.isoformat(),
                'file_type': 'audio',
                'file_url': msg.file.url if msg.file else None,
            }
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def export_deployment_excel(request):
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from django.http import HttpResponse
    from portal.models import DailyDeployment
    from django.db.models import Q
    from django.utils.timezone import localtime

    # Get filters
    q = request.GET.get('q', '').strip()
    machinery = request.GET.get('machinery', '').strip()
    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()

    queryset = DailyDeployment.objects.all().order_by('-date', '-id')

    # Apply filters
    if q:
        queryset = queryset.filter(
            Q(machinery__icontains=q) | 
            Q(date__icontains=q)
        )
    if machinery:
        queryset = queryset.filter(machinery__iexact=machinery)
    if start_date:
        queryset = queryset.filter(date__gte=start_date)
    if end_date:
        queryset = queryset.filter(date__lte=end_date)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily Deployment"
    ws.views.sheetView[0].showGridLines = True

    # Styles
    title_font = Font(name='Segoe UI', size=16, bold=True, color='1E3A8A')
    header_font = Font(name='Segoe UI', size=10, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1E3A8A', end_color='1E3A8A', fill_type='solid')
    align_center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    align_left = Alignment(horizontal='left', vertical='center')
    align_right = Alignment(horizontal='right', vertical='center')
    thin_border = Border(
        left=Side(style='thin', color='E5E7EB'),
        right=Side(style='thin', color='E5E7EB'),
        top=Side(style='thin', color='E5E7EB'),
        bottom=Side(style='thin', color='E5E7EB')
    )

    # Title info
    ws.merge_cells('A1:O1')
    ws['A1'] = "Plant & Machinery (P&M) Daily Deployment Report"
    ws['A1'].font = title_font
    ws['A1'].alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28

    ws.merge_cells('A2:O2')
    filter_desc = f"Export Date: {localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')}"
    if start_date or end_date:
        filter_desc += f" | Date Range: {start_date or 'All'} to {end_date or 'All'}"
    if machinery:
        filter_desc += f" | Vehicle: {machinery}"
    if q:
        filter_desc += f" | Search: {q}"
    ws['A2'] = filter_desc
    ws['A2'].font = Font(name='Segoe UI', size=9, italic=True, color='4B5563')
    ws['A2'].alignment = Alignment(horizontal='left', vertical='center')

    # Logo in P1:P2
    try:
        from fleet.views import _tyre_logo_image
        logo = _tyre_logo_image(width=52, height=52)
        if logo is not None:
            ws.merge_cells('P1:P2')
            ws.add_image(logo, 'P1')
    except Exception:
        pass

    # Table headers (Two rows for nested headers)
    ws.row_dimensions[4].height = 25
    ws.row_dimensions[5].height = 25

    headers = [
        ("DATE", "A4:A5"),
        ("MACHINERY / REG NO", "B4:B5"),
        ("ZONE 1 & 2", "C4:D4"),
        ("ZONE 3 & 4", "E4:F4"),
        ("BORROW AREA", "G4:H4"),
        ("CULVERT AREA", "I4:J4"),
        ("BATCHING PLANT", "K4:L4"),
        ("CRUSHING PLANT", "M4:N4"),
        ("ROAD MAINT", "O4:P4")
    ]

    subheaders = [
        ("Day", "C5"), ("Night", "D5"),
        ("Day", "E5"), ("Night", "F5"),
        ("Day", "G5"), ("Night", "H5"),
        ("Day", "I5"), ("Night", "J5"),
        ("Day", "K5"), ("Night", "L5"),
        ("Day", "M5"), ("Night", "N5"),
        ("Day", "O5"), ("Night", "P5")
    ]

    for title, span in headers:
        ws.merge_cells(span)
        cell = ws[span.split(':')[0]]
        cell.value = title
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    for title, cell_ref in subheaders:
        cell = ws[cell_ref]
        cell.value = title
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    # Add border and background to headers
    for row in ws.iter_rows(min_row=4, max_row=5, min_col=1, max_col=16):
        for cell in row:
            cell.border = thin_border
            if not cell.fill.fill_type:
                cell.fill = header_fill

    # Data rows
    row_num = 6
    for dep in queryset:
        ws.row_dimensions[row_num].height = 20
        data = [
            dep.date.strftime('%d-%m-%Y') if dep.date else '-',
            dep.machinery or '-',
            dep.zone_1_2_day, dep.zone_1_2_night,
            dep.zone_3_4_day, dep.zone_3_4_night,
            dep.borrow_area_day, dep.borrow_area_night,
            dep.culvert_area_day, dep.culvert_area_night,
            dep.batching_plant_day, dep.batching_plant_night,
            dep.crushing_plant_day, dep.crushing_plant_night,
            dep.road_maint_day, dep.road_maint_night
        ]

        for col_num, val in enumerate(data, start=1):
            cell = ws.cell(row=row_num, column=col_num, value=val)
            cell.border = thin_border
            cell.font = Font(name='Segoe UI', size=9)
            if col_num <= 2:
                cell.alignment = align_left if col_num == 2 else align_center
            else:
                cell.alignment = align_center
                
        # Alternating row background colors
        if row_num % 2 == 0:
            row_fill = PatternFill(start_color='F9FAFB', end_color='F9FAFB', fill_type='solid')
            for cell in ws[row_num]:
                if cell.column <= 16:
                    cell.fill = row_fill
                    
        row_num += 1

    # Auto-adjust column widths
    for col in ws.columns:
        if col[0].column > 16:
            continue
        max_len = 0
        for cell in col:
            val_str = str(cell.value or '')
            if len(val_str) > max_len:
                max_len = len(val_str)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = "attachment; filename=Daily_Deployment_Report.xlsx"
    wb.save(response)
    return response


@login_required
def export_allocation_excel(request):
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from django.http import HttpResponse
    from portal.models import DailyVehicleAllocation
    from django.db.models import Q
    from django.utils.timezone import localtime

    category = request.GET.get('category', '').strip()
    shift = request.GET.get('shift', '').strip()
    zone = request.GET.get('zone', '').strip()
    date_val = request.GET.get('date', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    driver = request.GET.get('driver', '').strip()
    drivers = request.GET.get('drivers', '').strip()
    q = request.GET.get('q', '').strip()

    qs = DailyVehicleAllocation.objects.select_related('entered_by', 'driver', 'vehicle').all().order_by('-date', 'shift', 'category', 'vehicle_regn')
    if category and category != 'All':
        qs = qs.filter(category=category)
    if shift and shift != 'All':
        qs = qs.filter(shift=shift)
    if zone and zone != 'All':
        qs = qs.filter(location_zone__icontains=zone)
    if date_val:
        qs = qs.filter(date=date_val)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if driver and driver != 'All':
        qs = qs.filter(Q(driver_name__icontains=driver) | Q(driver_emp_id__icontains=driver))
    if drivers and drivers != 'All':
        d_list = [d.strip() for d in drivers.split(',') if d.strip()]
        if d_list:
            d_q = Q()
            for d in d_list:
                d_q |= Q(driver_name__icontains=d) | Q(driver_emp_id__icontains=d)
            qs = qs.filter(d_q)
    if q:
        qs = qs.filter(
            Q(vehicle_regn__icontains=q) |
            Q(driver_name__icontains=q) |
            Q(driver_emp_id__icontains=q) |
            Q(location_zone__icontains=q) |
            Q(work_order_no__icontains=q) |
            Q(vendor__icontains=q)
        )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Vehicle Allocations"
    ws.views.sheetView[0].showGridLines = True

    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill(start_color='1E3A8A', end_color='1E3A8A', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF', size=10)

    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 20
    ws.row_dimensions[4].height = 26

    # Row 1: Project Title
    ws.merge_cells('A1:M1')
    ws['A1'] = "Proposed International Airport Project in Gelephu"
    ws['A1'].font = Font(bold=True, size=12)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')

    # Row 2: Subtitle
    ws.merge_cells('A2:L2')
    sub_title = "DAILY VEHICLE & DRIVER ALLOCATION REGISTER"
    if category and category != 'All':
        sub_title += f" ({category.upper()} DEPARTMENT)"
    ws['A2'] = sub_title
    ws['A2'].font = Font(bold=True, size=13)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')

    # Row 3: Filter info
    ws.merge_cells('A3:M3')
    f_info = f"Exported: {localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')}"
    if date_val: f_info += f" | Date: {date_val}"
    if date_from or date_to: f_info += f" | Date Range: {date_from or 'All'} to {date_to or 'All'}"
    if category and category != 'All': f_info += f" | Department: {category}"
    if shift and shift != 'All': f_info += f" | Shift: {shift}"
    if zone and zone != 'All': f_info += f" | Zone: {zone}"
    if driver and driver != 'All': f_info += f" | Driver: {driver}"
    if drivers and drivers != 'All': f_info += f" | Drivers: {drivers}"
    ws['A3'] = f_info
    ws['A3'].font = Font(size=9, italic=True, color='555555')
    ws['A3'].alignment = Alignment(horizontal='left', vertical='center')

    # Logo in M1:M2
    try:
        from fleet.views import _tyre_logo_image
        logo = _tyre_logo_image(width=52, height=52)
        if logo is not None:
            ws.merge_cells('M1:M2')
            ws.add_image(logo, 'M1')
    except Exception:
        pass

    headers = ['SL NO', 'DATE', 'SHIFT', 'IN TIME', 'OUT TIME', 'DEPARTMENT', 'VEHICLE REG NO', 'MODEL / EQUIPMENT', 'DRIVER / OPERATOR', 'EMP ID', 'CONTACT NO', 'LOCATION / ZONE', 'WO NO', 'VENDOR / OWNER', 'REMARKS', 'ENTERED BY']
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=4, column=ci, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = border

    row_num = 5
    for idx, a in enumerate(qs, 1):
        ws.row_dimensions[row_num].height = 20
        model_name = a.vehicle.model_name if a.vehicle else ''
        d_vals = [
            idx,
            a.date.strftime('%d.%m.%Y') if a.date else '',
            a.shift,
            a.in_time or '-',
            a.out_time or '-',
            a.category,
            a.vehicle_regn,
            model_name or '-',
            a.driver_name,
            a.driver_emp_id or (a.driver.emp_id if a.driver else '-'),
            a.driver_contact or (a.driver.contact_info if a.driver else '-'),
            a.location_zone,
            a.work_order_no or '-',
            a.vendor or '-',
            a.remarks or '-',
            a.entered_by.full_name or a.entered_by.username if a.entered_by else '-'
        ]
        for ci, val in enumerate(d_vals, 1):
            cell = ws.cell(row=row_num, column=ci, value=val)
            cell.border = border
            cell.font = Font(name='Segoe UI', size=9)
            if ci in [1, 2, 3, 4]:
                cell.alignment = Alignment(horizontal='center', vertical='center')
            else:
                cell.alignment = Alignment(horizontal='left', vertical='center')
        row_num += 1

    # Widths
    col_widths = [8, 12, 10, 14, 18, 20, 22, 16, 16, 20, 22, 24, 18, 16]
    for ci, w in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = w

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = "attachment; filename=Vehicle_Driver_Allocations.xlsx"
    wb.save(response)
    return response


@login_required
def export_deployment_pdf(request):
    from django.shortcuts import render
    from portal.models import DailyDeployment
    from django.db.models import Q
    from django.utils.timezone import localtime

    # Get filters
    q = request.GET.get('q', '').strip()
    machinery = request.GET.get('machinery', '').strip()
    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()

    queryset = DailyDeployment.objects.all().order_by('-date', '-id')

    # Apply filters
    if q:
        queryset = queryset.filter(
            Q(machinery__icontains=q) | 
            Q(date__icontains=q)
        )
    if machinery:
        queryset = queryset.filter(machinery__iexact=machinery)
    if start_date:
        queryset = queryset.filter(date__gte=start_date)
    if end_date:
        queryset = queryset.filter(date__lte=end_date)

    filter_desc = ""
    if start_date or end_date:
        filter_desc += f"Date Range: {start_date or 'All'} to {end_date or 'All'}"
    if machinery:
        filter_desc += f" | Vehicle: {machinery}"
    if q:
        filter_desc += f" | Search: {q}"

    return render(request, 'deployment_print.html', {
        'deployments': queryset,
        'filter_desc': filter_desc,
        'export_time': localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')
    })


@login_required
def export_allocation_pdf(request):
    from django.shortcuts import render
    from portal.models import DailyVehicleAllocation
    from django.db.models import Q
    from django.utils.timezone import localtime

    category = request.GET.get('category', '').strip()
    shift = request.GET.get('shift', '').strip()
    zone = request.GET.get('zone', '').strip()
    date_val = request.GET.get('date', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    driver = request.GET.get('driver', '').strip()
    drivers = request.GET.get('drivers', '').strip()
    q = request.GET.get('q', '').strip()

    qs = DailyVehicleAllocation.objects.select_related('entered_by', 'driver', 'vehicle').all().order_by('-date', 'shift', 'category', 'vehicle_regn')
    if category and category != 'All':
        qs = qs.filter(category=category)
    if shift and shift != 'All':
        qs = qs.filter(shift=shift)
    if zone and zone != 'All':
        qs = qs.filter(location_zone__icontains=zone)
    if date_val:
        qs = qs.filter(date=date_val)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if driver and driver != 'All':
        qs = qs.filter(Q(driver_name__icontains=driver) | Q(driver_emp_id__icontains=driver))
    if drivers and drivers != 'All':
        d_list = [d.strip() for d in drivers.split(',') if d.strip()]
        if d_list:
            d_q = Q()
            for d in d_list:
                d_q |= Q(driver_name__icontains=d) | Q(driver_emp_id__icontains=d)
            qs = qs.filter(d_q)
    if q:
        qs = qs.filter(
            Q(vehicle_regn__icontains=q) |
            Q(driver_name__icontains=q) |
            Q(driver_emp_id__icontains=q) |
            Q(location_zone__icontains=q) |
            Q(work_order_no__icontains=q) |
            Q(vendor__icontains=q)
        )

    filter_desc_parts = []
    if date_val: filter_desc_parts.append(f"Date: {date_val}")
    if date_from or date_to: filter_desc_parts.append(f"Date Range: {date_from or 'Start'} to {date_to or 'End'}")
    if category and category != 'All': filter_desc_parts.append(f"Department: {category}")
    if shift and shift != 'All': filter_desc_parts.append(f"Shift: {shift}")
    if zone and zone != 'All': filter_desc_parts.append(f"Zone: {zone}")
    if driver and driver != 'All': filter_desc_parts.append(f"Driver: {driver}")
    if drivers and drivers != 'All': filter_desc_parts.append(f"Drivers: {drivers}")
    if q: filter_desc_parts.append(f"Search: '{q}'")
    filter_desc = " | ".join(filter_desc_parts) if filter_desc_parts else "All Records"

    return render(request, 'allocation_print.html', {
        'allocations': qs,
        'category': category,
        'filter_desc': filter_desc,
        'export_time': localtime(localtime()).strftime('%d-%m-%Y %I:%M %p')
    })


@login_required
def deployment_export_hub_view(request):
    import datetime
    today = datetime.date.today()
    category = request.GET.get('category', 'All').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    driver = request.GET.get('driver', 'All').strip()
    drivers = request.GET.get('drivers', '').strip()
    q = request.GET.get('q', '').strip()

    qs = DailyVehicleAllocation.objects.select_related('entered_by', 'driver', 'vehicle').all().order_by('-date', 'shift', 'category', 'vehicle_regn')
    if category and category != 'All':
        qs = qs.filter(category=category)
    if shift and shift != 'All':
        qs = qs.filter(shift=shift)
    if zone and zone != 'All':
        qs = qs.filter(location_zone__icontains=zone)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if driver and driver != 'All':
        qs = qs.filter(Q(driver_name__icontains=driver) | Q(driver_emp_id__icontains=driver))
    if drivers and drivers != 'All':
        d_list = [d.strip() for d in drivers.split(',') if d.strip()]
        if d_list:
            d_q = Q()
            for d in d_list:
                d_q |= Q(driver_name__icontains=d) | Q(driver_emp_id__icontains=d)
            qs = qs.filter(d_q)
    if q:
        qs = qs.filter(
            Q(vehicle_regn__icontains=q) |
            Q(driver_name__icontains=q) |
            Q(driver_emp_id__icontains=q) |
            Q(location_zone__icontains=q) |
            Q(work_order_no__icontains=q) |
            Q(vendor__icontains=q)
        )

    return render(request, 'deployment_export.html', {
        'allocations': qs[:500],
        'total_count': qs.count(),
        'category': category,
        'shift': shift,
        'zone': zone,
        'date_from': date_from,
        'date_to': date_to,
        'driver': driver,
        'drivers': drivers,
        'q': q,
        'today': today,
    })


def _get_user_allowed_activity_modules(user):
    """Returns the list of module names that the user is permitted to view."""
    if user.is_superuser or (user.system_role == 'MANAGER' and not getattr(user, 'assigned_modules', None)):
        return [
            'Vehicle Allocation',
            'Daily Deployment',
            'Spare Parts',
            'Tyre Register',
            'Vehicle Movement',
            'Lubricants',
            'Shifts',
            'Document Alerts',
            'Auth',
        ]
    
    assigned = getattr(user, 'assigned_modules', []) or []
    module_map = {
        'daily_deployment': ['Vehicle Allocation', 'Daily Deployment'],
        'vehicle_movement': ['Vehicle Movement'],
        'spare_parts': ['Spare Parts'],
        'fleet_spare_parts': ['Spare Parts'],
        'tyre_section': ['Tyre Register'],
        'fleet_tyre': ['Tyre Register'],
        'lubricants': ['Lubricants'],
        'shift_management': ['Shifts'],
        'document_expiries': ['Document Alerts'],
    }
    allowed = []
    for mod_key in assigned:
        for mapped in module_map.get(mod_key, []):
            if mapped not in allowed:
                allowed.append(mapped)
                
    if not allowed:
        allowed = ['Vehicle Allocation', 'Daily Deployment', 'Spare Parts', 'Tyre Register', 'Auth']
        
    return allowed


def _estimate_user_active_minutes(timestamps):
    """Calculates active minutes by clustering timestamps within 15-minute windows."""
    if not timestamps:
        return 0
    sorted_ts = sorted(timestamps)
    total_minutes = 0
    last_ts = sorted_ts[0]
    for ts in sorted_ts[1:]:
        diff_minutes = (ts - last_ts).total_seconds() / 60.0
        if diff_minutes <= 15.0:
            total_minutes += diff_minutes
        else:
            total_minutes += 3.0
        last_ts = ts
    total_minutes += 3.0
    return round(total_minutes)


def _format_duration_mins(minutes):
    if minutes <= 0:
        return "0m"
    hrs = int(minutes // 60)
    mins = int(minutes % 60)
    if hrs > 0 and mins > 0:
        return f"{hrs}h {mins}m"
    elif hrs > 0:
        return f"{hrs}h"
    else:
        return f"{mins}m"


def _compute_executive_metrics(logs_qs, target_users_qs):
    now = timezone.now()
    cutoff_active = now - timedelta(minutes=5)
    cutoff_idle = now - timedelta(minutes=30)
    
    total_users = target_users_qs.count()
    active_users = 0
    idle_users = 0
    offline_users = 0
    
    for u in target_users_qs:
        if u.last_seen:
            if u.last_seen >= cutoff_active:
                active_users += 1
            elif u.last_seen >= cutoff_idle:
                idle_users += 1
            else:
                offline_users += 1
        else:
            offline_users += 1
            
    total_logins = logs_qs.filter(action_type='LOGIN').count()
    corrections = logs_qs.filter(action_type='UPDATE').count()
    creates = logs_qs.filter(action_type='CREATE').count()
    deletes = logs_qs.filter(action_type='DELETE').count()
    approves = logs_qs.filter(action_type='APPROVE').count()
    total_actions = logs_qs.count()
    
    if total_actions > 0:
        first_log = logs_qs.order_by('created_at').first()
        last_log = logs_qs.order_by('-created_at').first()
        if first_log and last_log and first_log.created_at != last_log.created_at:
            span_hrs = max(0.5, (last_log.created_at - first_log.created_at).total_seconds() / 3600.0)
            avg_speed_str = f"{round(total_actions / span_hrs, 1)} entries/hr"
        else:
            avg_speed_str = f"{total_actions} entries"
    else:
        avg_speed_str = "0 entries"

    return {
        'total_users': total_users,
        'active_users': active_users,
        'idle_users': idle_users,
        'offline_users': offline_users,
        'total_logins': total_logins,
        'corrections': corrections,
        'creates': creates,
        'deletes': deletes,
        'approves': approves,
        'total_actions': total_actions,
        'avg_speed': avg_speed_str,
    }


@login_required
def user_activity_view(request):
    import datetime
    from django.utils.timesince import timesince
    
    can_view = request.user.is_superuser or bool(getattr(request.user, 'can_view_user_activity', False))
    if not can_view:
        messages.error(request, "Permission denied: You do not have permission to view User Activity.")
        return redirect('dashboard')

    monitors_ids = User.objects.filter(
        Q(is_superuser=True) | Q(can_view_user_activity=True)
    ).values_list('id', flat=True)

    allowed_modules = _get_user_allowed_activity_modules(request.user)
    logs = UserActivityLog.objects.exclude(user_id__in=monitors_ids)
    
    if allowed_modules:
        logs = logs.filter(module_name__in=allowed_modules)

    user_captain_cat = getattr(request.user, 'captain_category', 'All') or 'All'
    if not request.user.is_superuser and request.user.system_role != 'MANAGER' and user_captain_cat != 'All':
        logs = logs.filter(Q(description__icontains=user_captain_cat) | Q(module_name__in=['Vehicle Allocation', 'Daily Deployment']))

    user_id = request.GET.get('user_id')
    role = request.GET.get('role')
    action_type = request.GET.get('action_type')
    module_name = request.GET.get('module_name')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    q = request.GET.get('q')

    if user_id:
        logs = logs.filter(user_id=user_id)
    if role:
        logs = logs.filter(user_role=role)
    if action_type:
        logs = logs.filter(action_type=action_type)
    if module_name:
        logs = logs.filter(module_name=module_name)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    if q:
        logs = logs.filter(
            Q(user_name__icontains=q) |
            Q(description__icontains=q) |
            Q(module_name__icontains=q)
        )

    all_target_users = User.objects.exclude(id__in=monitors_ids).order_by('full_name', 'username')
    summary_stats = _compute_executive_metrics(logs, all_target_users)

    # User Summary with Online Active Durations
    user_timestamps_map = {}
    for log_item in logs:
        if log_item.user_id:
            user_timestamps_map.setdefault(log_item.user_id, []).append(log_item.created_at)

    now = timezone.now()
    cutoff_active = now - timedelta(minutes=5)
    cutoff_idle = now - timedelta(minutes=30)
    
    user_summary_list = []
    for u in all_target_users:
        u_ts = user_timestamps_map.get(u.id, [])
        u_active_mins = _estimate_user_active_minutes(u_ts)
        is_online = bool(u.last_seen and u.last_seen >= cutoff_active)
        is_idle = bool(u.last_seen and cutoff_idle <= u.last_seen < cutoff_active)
        
        u_creates = logs.filter(user_id=u.id, action_type='CREATE').count()
        u_updates = logs.filter(user_id=u.id, action_type='UPDATE').count()
        u_deletes = logs.filter(user_id=u.id, action_type='DELETE').count()
        u_logins = logs.filter(user_id=u.id, action_type='LOGIN').count()
        u_total = logs.filter(user_id=u.id).count()

        # Seamless aggregation from real operational tables if UserActivityLog has 0 entries
        if u_total == 0:
            try:
                from portal.models import DailyDeployment, DailyVehicleAllocation, SectionEntry, OvertimeRecord
                dep_cnt = DailyDeployment.objects.filter(entered_by=u).count()
                sec_cnt = SectionEntry.objects.filter(created_by=u).count()
                ot_cnt = OvertimeRecord.objects.filter(time_keeper=u).count()
                u_creates = dep_cnt + sec_cnt + ot_cnt
                u_logins = 1 if u.last_seen else 0
                u_total = u_creates + u_logins
                if u_total > 0 and u_active_mins == 0:
                    u_active_mins = max(15, u_total * 4)
            except Exception:
                pass
        
        user_summary_list.append({
            'id': u.id,
            'name': u.full_name or u.username,
            'username': u.username,
            'role': u.system_role,
            'is_online': is_online,
            'is_idle': is_idle,
            'last_seen': localtime(u.last_seen).strftime('%d %b, %I:%M %p') if u.last_seen else 'Never',
            'active_mins': u_active_mins,
            'estimated_active_time': _format_duration_mins(u_active_mins),
            'logins': u_logins,
            'creates': u_creates,
            'updates': u_updates,
            'deletes': u_deletes,
            'total_actions': u_total,
        })

    from django.core.paginator import Paginator
    paginator = Paginator(logs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    selected_user_obj = None
    selected_user_metrics = None
    selected_user_timeline = []
    if user_id:
        selected_user_obj = User.objects.filter(id=user_id).first()
        if selected_user_obj:
            u_logs = logs.filter(user_id=user_id)
            u_ts = user_timestamps_map.get(selected_user_obj.id, [])
            u_active_mins = _estimate_user_active_minutes(u_ts)
            selected_user_metrics = {
                'total': u_logs.count(),
                'creates': u_logs.filter(action_type='CREATE').count(),
                'updates': u_logs.filter(action_type='UPDATE').count(),
                'deletes': u_logs.filter(action_type='DELETE').count(),
                'approves': u_logs.filter(action_type='APPROVE').count(),
                'logins': u_logs.filter(action_type='LOGIN').count(),
                'estimated_active_time': _format_duration_mins(u_active_mins),
            }
            for l in u_logs[:60]:
                t_ago = timesince(l.created_at).split(',')[0] + ' ago' if l.created_at else ''
                if '0\xa0minutes' in t_ago or '0 minutes' in t_ago:
                    t_ago = 'Just now'
                selected_user_timeline.append({
                    'id': l.id,
                    'action_type': l.action_type,
                    'module_name': l.module_name,
                    'description': l.description,
                    'date_str': localtime(l.created_at).strftime('%d %b %Y'),
                    'time_str': localtime(l.created_at).strftime('%I:%M %p'),
                    'time_ago': t_ago,
                })

    context = {
        'page_obj': page_obj,
        'logs': page_obj.object_list,
        'target_users': all_target_users,
        'summary_stats': summary_stats,
        'user_summary_list': user_summary_list,
        'available_modules': allowed_modules,
        'selected_user_id': user_id or '',
        'selected_user_obj': selected_user_obj,
        'selected_user_metrics': selected_user_metrics,
        'selected_user_timeline': selected_user_timeline,
        'selected_role': role or '',
        'selected_action_type': action_type or '',
        'selected_module_name': module_name or '',
        'date_from': date_from or '',
        'date_to': date_to or '',
        'q': q or '',
    }
    return render(request, 'user_activity.html', context)


@login_required
def export_user_activity(request):
    can_view = request.user.is_superuser or bool(getattr(request.user, 'can_view_user_activity', False))
    if not can_view:
        return HttpResponse('Unauthorized: You do not have permission to export user activity.', status=403)

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from django.http import HttpResponse

    monitors_ids = User.objects.filter(
        Q(is_superuser=True) | Q(can_view_user_activity=True)
    ).values_list('id', flat=True)

    allowed_modules = _get_user_allowed_activity_modules(request.user)
    logs = UserActivityLog.objects.exclude(user_id__in=monitors_ids)
    if allowed_modules:
        logs = logs.filter(module_name__in=allowed_modules)

    user_captain_cat = getattr(request.user, 'captain_category', 'All') or 'All'
    if not request.user.is_superuser and request.user.system_role != 'MANAGER' and user_captain_cat != 'All':
        logs = logs.filter(Q(description__icontains=user_captain_cat) | Q(module_name__in=['Vehicle Allocation', 'Daily Deployment']))

    user_id = request.GET.get('user_id')
    role = request.GET.get('role')
    action_type = request.GET.get('action_type')
    module_name = request.GET.get('module_name')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    q = request.GET.get('q')

    if user_id:
        logs = logs.filter(user_id=user_id)
    if role:
        logs = logs.filter(user_role=role)
    if action_type:
        logs = logs.filter(action_type=action_type)
    if module_name:
        logs = logs.filter(module_name=module_name)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    if q:
        logs = logs.filter(
            Q(user_name__icontains=q) |
            Q(description__icontains=q) |
            Q(module_name__icontains=q)
        )

    all_target_users = User.objects.exclude(id__in=monitors_ids).order_by('full_name', 'username')
    summary_stats = _compute_executive_metrics(logs, all_target_users)

    wb = openpyxl.Workbook()
    
    # Sheet 1: Executive KPI & User Productivity Summary
    ws1 = wb.active
    ws1.title = "Executive Summary"
    
    ws1.append(["P&M SYSTEM - USER ACTIVITY & PRODUCTIVITY REPORT"])
    ws1.append([f"Report Generated: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"])
    ws1.append([])
    
    ws1.append(["EXECUTIVE KPI METRICS"])
    ws1.append(["Total Users", "Active Users", "Idle Users", "Offline Users", "Total Logins", "Corrections", "Avg Entry Speed", "Total Logs"])
    ws1.append([
        summary_stats['total_users'],
        summary_stats['active_users'],
        summary_stats['idle_users'],
        summary_stats['offline_users'],
        summary_stats['total_logins'],
        summary_stats['corrections'],
        summary_stats['avg_speed'],
        summary_stats['total_actions'],
    ])
    ws1.append([])
    
    ws1.append(["USER PRODUCTIVITY & ACTIVE TIME BREAKDOWN"])
    user_headers = ["SR NO", "USER NAME", "ROLE", "ESTIMATED ACTIVE TIME", "LOGINS", "CREATED", "UPDATED (CORRECTIONS)", "DELETED", "TOTAL ACTIONS", "LAST ACTIVE"]
    ws1.append(user_headers)
    
    user_timestamps_map = {}
    for log_item in logs:
        if log_item.user_id:
            user_timestamps_map.setdefault(log_item.user_id, []).append(log_item.created_at)

    for idx, u in enumerate(all_target_users, 1):
        u_ts = user_timestamps_map.get(u.id, [])
        u_active_mins = _estimate_user_active_minutes(u_ts)
        u_creates = logs.filter(user_id=u.id, action_type='CREATE').count()
        u_updates = logs.filter(user_id=u.id, action_type='UPDATE').count()
        u_deletes = logs.filter(user_id=u.id, action_type='DELETE').count()
        u_logins = logs.filter(user_id=u.id, action_type='LOGIN').count()
        u_total = logs.filter(user_id=u.id).count()
        
        ws1.append([
            idx,
            u.full_name or u.username,
            u.system_role,
            _format_duration_mins(u_active_mins),
            u_logins,
            u_creates,
            u_updates,
            u_deletes,
            u_total,
            localtime(u.last_seen).strftime('%Y-%m-%d %H:%M') if u.last_seen else 'Never'
        ])

    # Sheet 2: Chronological Activity Trail
    ws2 = wb.create_sheet(title="Chronological Audit Trail")
    trail_headers = ['SR NO', 'DATE & TIME', 'USER NAME', 'ROLE', 'ACTION TYPE', 'MODULE', 'DESCRIPTION']
    ws2.append(trail_headers)

    header_fill = PatternFill(start_color='1E293B', end_color='1E293B', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)
    for col_idx in range(1, len(trail_headers) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')

    for idx, log in enumerate(logs[:4000], 1):
        dt_str = localtime(log.created_at).strftime('%Y-%m-%d %H:%M:%S') if log.created_at else ''
        ws2.append([
            idx,
            dt_str,
            log.user_name,
            log.user_role,
            log.action_type,
            log.module_name,
            log.description,
        ])

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="User_Activity_Report_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
    wb.save(response)
    return response


@login_required
def export_user_activity_pdf(request):
    """Render print-ready HTML for browser PDF generation with complete analytics & timeline."""
    import datetime
    
    can_view = request.user.is_superuser or bool(getattr(request.user, 'can_view_user_activity', False))
    if not can_view:
        return HttpResponse('Unauthorized: You do not have permission to export user activity PDF.', status=403)

    monitors_ids = User.objects.filter(
        Q(is_superuser=True) | Q(can_view_user_activity=True)
    ).values_list('id', flat=True)

    allowed_modules = _get_user_allowed_activity_modules(request.user)
    logs = UserActivityLog.objects.exclude(user_id__in=monitors_ids)
    if allowed_modules:
        logs = logs.filter(module_name__in=allowed_modules)

    user_captain_cat = getattr(request.user, 'captain_category', 'All') or 'All'
    if not request.user.is_superuser and request.user.system_role != 'MANAGER' and user_captain_cat != 'All':
        logs = logs.filter(Q(description__icontains=user_captain_cat) | Q(module_name__in=['Vehicle Allocation', 'Daily Deployment']))

    user_id = request.GET.get('user_id')
    role = request.GET.get('role')
    action_type = request.GET.get('action_type')
    module_name = request.GET.get('module_name')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    q = request.GET.get('q')

    filter_desc_parts = []
    if user_id:
        logs = logs.filter(user_id=user_id)
        u_obj = User.objects.filter(id=user_id).first()
        if u_obj: filter_desc_parts.append(f"User: {u_obj.full_name or u_obj.username}")
    if role:
        logs = logs.filter(user_role=role)
        filter_desc_parts.append(f"Role: {role}")
    if action_type:
        logs = logs.filter(action_type=action_type)
        filter_desc_parts.append(f"Action: {action_type}")
    if module_name:
        logs = logs.filter(module_name=module_name)
        filter_desc_parts.append(f"Module: {module_name}")
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
        filter_desc_parts.append(f"From: {date_from}")
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
        filter_desc_parts.append(f"To: {date_to}")
    if q:
        logs = logs.filter(
            Q(user_name__icontains=q) |
            Q(description__icontains=q) |
            Q(module_name__icontains=q)
        )
        filter_desc_parts.append(f"Search: '{q}'")

    filter_desc = " | ".join(filter_desc_parts) if filter_desc_parts else "All Permitted Activity"

    all_target_users = User.objects.exclude(id__in=monitors_ids).order_by('full_name', 'username')
    summary_stats = _compute_executive_metrics(logs, all_target_users)

    user_timestamps_map = {}
    for log_item in logs:
        if log_item.user_id:
            user_timestamps_map.setdefault(log_item.user_id, []).append(log_item.created_at)

    now = timezone.now()
    cutoff_active = now - timedelta(minutes=5)
    cutoff_idle = now - timedelta(minutes=30)
    
    user_summary_list = []
    for u in all_target_users:
        u_ts = user_timestamps_map.get(u.id, [])
        u_active_mins = _estimate_user_active_minutes(u_ts)
        is_online = bool(u.last_seen and u.last_seen >= cutoff_active)
        is_idle = bool(u.last_seen and cutoff_idle <= u.last_seen < cutoff_active)
        
        u_creates = logs.filter(user_id=u.id, action_type='CREATE').count()
        u_updates = logs.filter(user_id=u.id, action_type='UPDATE').count()
        u_deletes = logs.filter(user_id=u.id, action_type='DELETE').count()
        u_logins = logs.filter(user_id=u.id, action_type='LOGIN').count()
        u_total = logs.filter(user_id=u.id).count()
        
        user_summary_list.append({
            'name': u.full_name or u.username,
            'role': u.system_role,
            'is_online': is_online,
            'is_idle': is_idle,
            'last_seen': localtime(u.last_seen).strftime('%d %b, %I:%M %p') if u.last_seen else 'Never',
            'estimated_active_time': _format_duration_mins(u_active_mins),
            'logins': u_logins,
            'creates': u_creates,
            'updates': u_updates,
            'deletes': u_deletes,
            'total_actions': u_total,
        })

    context = {
        'logs': logs[:500],
        'summary_stats': summary_stats,
        'user_summary_list': user_summary_list,
        'filter_desc': filter_desc,
        'report_generated_at': timezone.now(),
    }
    return render(request, 'user_activity_pdf.html', context)


@login_required
def api_live_user_activity(request):
    import datetime
    from django.utils.timesince import timesince
    
    can_view = request.user.is_superuser or bool(getattr(request.user, 'can_view_user_activity', False))
    if not can_view:
        return JsonResponse({'status': 'error', 'message': 'Unauthorized: Permission denied.'}, status=403)

    monitors_ids = User.objects.filter(
        Q(is_superuser=True) | Q(can_view_user_activity=True)
    ).values_list('id', flat=True)

    target_users = User.objects.exclude(id__in=monitors_ids).order_by('full_name', 'username')
    
    allowed_modules = _get_user_allowed_activity_modules(request.user)
    logs = UserActivityLog.objects.exclude(user_id__in=monitors_ids)
    if allowed_modules:
        logs = logs.filter(module_name__in=allowed_modules)

    user_captain_cat = getattr(request.user, 'captain_category', 'All') or 'All'
    if not request.user.is_superuser and request.user.system_role != 'MANAGER' and user_captain_cat != 'All':
        logs = logs.filter(Q(description__icontains=user_captain_cat) | Q(module_name__in=['Vehicle Allocation', 'Daily Deployment']))
    
    user_id = request.GET.get('user_id')
    role = request.GET.get('role')
    action_type = request.GET.get('action_type')
    module_name = request.GET.get('module_name')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    q = request.GET.get('q')

    if user_id:
        logs = logs.filter(user_id=user_id)
    if role:
        logs = logs.filter(user_role=role)
    if action_type:
        logs = logs.filter(action_type=action_type)
    if module_name:
        logs = logs.filter(module_name=module_name)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    if q:
        logs = logs.filter(
            Q(user_name__icontains=q) |
            Q(description__icontains=q) |
            Q(module_name__icontains=q)
        )

    summary_stats = _compute_executive_metrics(logs, target_users)

    user_timestamps_map = {}
    for log_item in logs:
        if log_item.user_id:
            user_timestamps_map.setdefault(log_item.user_id, []).append(log_item.created_at)

    now = timezone.now()
    cutoff_active = now - timedelta(minutes=5)
    cutoff_idle = now - timedelta(minutes=30)
    
    users_data = []
    for u in target_users:
        u_ts = user_timestamps_map.get(u.id, [])
        u_active_mins = _estimate_user_active_minutes(u_ts)
        is_online = bool(u.last_seen and u.last_seen >= cutoff_active)
        is_idle = bool(u.last_seen and cutoff_idle <= u.last_seen < cutoff_active)
        
        users_data.append({
            'id': u.id,
            'name': u.full_name or u.username,
            'role': u.system_role,
            'is_online': is_online,
            'is_idle': is_idle,
            'estimated_active_time': _format_duration_mins(u_active_mins),
            'last_seen': localtime(u.last_seen).strftime('%d %b, %H:%M') if u.last_seen else 'Never'
        })

    latest_logs_data = []
    for log in logs[:50]:
        t_ago = timesince(log.created_at).split(',')[0] + ' ago' if log.created_at else ''
        if '0\xa0minutes' in t_ago or '0 minutes' in t_ago:
            t_ago = 'Just now'
            
        latest_logs_data.append({
            'id': log.id,
            'user_name': log.user_name,
            'user_id': log.user_id,
            'user_role': log.user_role,
            'action_type': log.action_type,
            'module_name': log.module_name,
            'description': log.description,
            'date_str': localtime(log.created_at).strftime('%d %b %Y') if log.created_at else '',
            'time_str': localtime(log.created_at).strftime('%I:%M %p') if log.created_at else '',
            'time_ago': t_ago,
        })

    return JsonResponse({
        'status': 'success',
        'summary_stats': summary_stats,
        'users': users_data,
        'logs': latest_logs_data,
        'total_count': logs.count(),
    })


# ==============================================================================
# OVERTIME MANAGEMENT SYSTEM (Time Keeper QR Scanner & PM Dashboard)
# ==============================================================================

def user_has_overtime_permission(user, is_timekeeper_scanner=False):
    """
    Checks if a user has permission to access Overtime Management.
    - Superuser: Always has full access.
    - Specific Manager/User: MUST have 'overtime_management' in assigned_modules.
    - Time Keeper: Allowed to access scanner only.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    assigned = getattr(user, 'assigned_modules', None) or []
    if 'overtime_management' in assigned:
        return True
    if is_timekeeper_scanner and user.system_role == 'TIME_KEEPER':
        return True
    return False


@login_required
def overtime_scanner_view(request):
    """Mobile-friendly QR code & last-digit scanner for Time Keepers and authorized Managers."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=True):
        messages.error(request, "Permission denied: You do not have permission to access Overtime Scanner.")
        return redirect('dashboard')

    today = timezone.localdate()
    zones = [
        'Zone 1 & 2', 'Crushing Plant', 'Batching Plant', 'Borrow Area',
        'Dam Site', 'Power House', 'Workshop / Garage', 'Road Maintenance', 'Other Location'
    ]
    
    # Fetch today's punches logged by this time keeper
    recent_punches = OvertimeRecord.objects.filter(
        date=today, time_keeper=request.user
    ).order_by('-created_at')[:30] if not request.user.is_superuser else OvertimeRecord.objects.filter(date=today).order_by('-created_at')[:30]
    
    total_logged_today = recent_punches.count()
    total_hours_today = sum(r.overtime_hours for r in recent_punches)
    
    context = {
        'today': today,
        'zones': zones,
        'recent_punches': recent_punches,
        'total_logged_today': total_logged_today,
        'total_hours_today': round(total_hours_today, 1),
    }
    return render(request, 'overtime_scan.html', context)


def parse_time_diff_hours(in_time_str, out_time_str):
    """Accurately parses in_time and out_time strings and returns exact duration in hours (rounded to 2 decimal places)."""
    if not in_time_str or not out_time_str:
        return 0.0
    
    in_clean = str(in_time_str).strip()
    out_clean = str(out_time_str).strip()
    if not in_clean or not out_clean or in_clean == '-' or out_clean == '-':
        return 0.0

    def to_minutes(t_str):
        m = re.search(r'(\d{1,2})[:.](\d{2})(?::\d{2})?\s*(AM|PM)?', t_str, re.IGNORECASE)
        if not m:
            return None
        h = int(m.group(1))
        mins = int(m.group(2))
        ampm = (m.group(3) or '').upper()
        if ampm == 'PM' and h < 12:
            h += 12
        elif ampm == 'AM' and h == 12:
            h = 0
        return h * 60 + mins

    m1 = to_minutes(in_clean)
    m2 = to_minutes(out_clean)
    if m1 is not None and m2 is not None:
        diff_mins = m2 - m1
        if diff_mins < 0:
            diff_mins += 24 * 60  # Overnight cross-midnight shift
        hours = diff_mins / 60.0
        return round(hours, 2)
    return 0.0



@login_required
def overtime_dashboard_view(request):
    """Executive Overtime & Productivity Dashboard for Project Manager and authorized Admins."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        if request.user.system_role == 'TIME_KEEPER':
            messages.warning(request, "Time Keepers only have access to QR Scanner.")
            return redirect('overtime_scan')
        messages.error(request, "Permission denied: You do not have permission to access Overtime Management.")
        return redirect('dashboard')

    today = timezone.localdate()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    status = request.GET.get('status', 'All').strip()
    time_keeper_id = request.GET.get('time_keeper', 'All').strip()
    q = request.GET.get('q', '').strip()

    qs = OvertimeRecord.objects.select_related('employee', 'time_keeper', 'approved_by').all().order_by('-date', '-created_at')

    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if shift and shift != 'All':
        qs = qs.filter(shift=shift)
    if zone and zone != 'All':
        qs = qs.filter(location_zone=zone)
    if status and status != 'All':
        qs = qs.filter(status=status)
    if time_keeper_id and time_keeper_id != 'All':
        qs = qs.filter(time_keeper_id=time_keeper_id)
    if q:
        qs = qs.filter(
            Q(emp_id_snapshot__icontains=q) |
            Q(employee_name__icontains=q) |
            Q(cid_number__icontains=q) |
            Q(account_number__icontains=q) |
            Q(designation__icontains=q) |
            Q(department__icontains=q) |
            Q(work_description__icontains=q) |
            Q(vehicle_regn__icontains=q)
        )

    # Executive KPI calculations
    total_records = qs.count()
    total_hours = sum(r.overtime_hours for r in qs)
    total_amount = sum(r.overtime_amount for r in qs)
    unique_workers = qs.values('emp_id_snapshot').distinct().count()
    pending_count = qs.filter(status='Pending').count()
    approved_count = qs.filter(status='Approved').count()
    active_time_keepers = qs.values('time_keeper_name').distinct().count()

    from django.core.paginator import Paginator
    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    all_time_keepers = User.objects.filter(
        Q(system_role='TIME_KEEPER') |
        Q(assigned_modules__icontains='overtime_management')
    ).filter(is_active=True).distinct().order_by('full_name', 'username')
    zones = [
        "Zone 1 & 2", "Zone 3 & 4", "Zone 5 & 6", "Workshop", "Yard / Batching Plant", "Crusher Unit", "Office / Camp Area", "Site Security"
    ]
    shifts = ["Day", "Night", "General"]
    db_shifts = list(OvertimeRecord.objects.values_list('shift', flat=True).distinct())
    for s in db_shifts:
        if s and s not in shifts:
            shifts.append(s)
    is_time_keeper = (request.user.system_role == 'TIME_KEEPER')

    context = {
        'page_obj': page_obj,
        'records': page_obj.object_list,
        'total_records': total_records,
        'total_hours': round(total_hours, 1),
        'total_amount': '***' if is_time_keeper else round(total_amount, 2),
        'unique_workers': unique_workers,
        'pending_count': pending_count,
        'approved_count': approved_count,
        'active_time_keepers': active_time_keepers,
        'all_time_keepers': all_time_keepers,
        'zones': zones,
        'shifts': shifts,
        'date_from': date_from,
        'date_to': date_to,
        'shift': shift,
        'zone': zone,
        'status': status,
        'time_keeper_id': time_keeper_id,
        'q': q,
        'today': today,
    }
    return render(request, 'overtime_dashboard.html', context)


@login_required
def api_live_overtime_data(request):
    """
    Live JSON feed for instantaneous live search, real-time filtering, 
    and live auto-synchronization with active Time Keeper scanners.
    """
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({'status': 'error', 'message': 'Permission denied: Unauthorized.'}, status=403)

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    status = request.GET.get('status', 'All').strip()
    time_keeper_id = request.GET.get('time_keeper', 'All').strip()
    q = request.GET.get('q', '').strip()

    qs = OvertimeRecord.objects.select_related('employee', 'time_keeper', 'approved_by').all().order_by('-date', '-created_at')

    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if shift and shift != 'All':
        qs = qs.filter(shift=shift)
    if zone and zone != 'All':
        qs = qs.filter(location_zone=zone)
    if status and status != 'All':
        qs = qs.filter(status=status)
    if time_keeper_id and time_keeper_id != 'All':
        qs = qs.filter(time_keeper_id=time_keeper_id)
    if q:
        qs = qs.filter(
            Q(emp_id_snapshot__icontains=q) |
            Q(employee_name__icontains=q) |
            Q(cid_number__icontains=q) |
            Q(account_number__icontains=q) |
            Q(designation__icontains=q) |
            Q(department__icontains=q) |
            Q(work_description__icontains=q) |
            Q(vehicle_regn__icontains=q)
        )

    # Executive KPI calculations
    total_records = qs.count()
    total_hours = sum(r.overtime_hours for r in qs)
    total_amount = sum(r.overtime_amount for r in qs)
    unique_workers = qs.values('emp_id_snapshot').distinct().count()
    pending_count = qs.filter(status='Pending').count()
    approved_count = qs.filter(status='Approved').count()

    is_time_keeper = (request.user.system_role == 'TIME_KEEPER')

    records_list = []
    for r in qs[:150]:
        tot_min = int(round((r.overtime_hours or 0.0) * 60))
        h_part = tot_min // 60
        m_part = tot_min % 60
        human_str = f"{h_part}h {m_part}m" if m_part > 0 else f"{h_part}h"

        records_list.append({
            'id': r.id,
            'date': r.date.strftime('%d %b %Y') if r.date else '',
            'created_at': localtime(r.created_at).strftime('%I:%M %p') if r.created_at else '',
            'emp_id': r.emp_id_snapshot,
            'name': r.employee_name,
            'initial': (r.employee_name or 'E')[:1].upper(),
            'designation': r.designation,
            'department': r.department,
            'cid_number': '-' if is_time_keeper else (r.cid_number or (r.employee.cid_number if r.employee else '') or '-'),
            'account_number': '-' if is_time_keeper else (r.account_number or (r.employee.account_number if r.employee else '') or '-'),
            'shift': r.shift,
            'zone': r.location_zone,
            'in_time': r.in_time or '-',
            'out_time': r.out_time or '-',
            'hours': r.overtime_hours,
            'hours_human': human_str,
            'rate': 0.0 if is_time_keeper else r.overtime_rate,
            'amount': 0.0 if is_time_keeper else r.overtime_amount,
            'time_keeper_name': r.time_keeper_name or 'Self',
            'status': r.status,
            'work_description': r.work_description or '',
        })

    max_id = qs.aggregate(m=Max('id'))['m'] or 0

    return JsonResponse({
        'status': 'success',
        'records': records_list,
        'kpis': {
            'total_records': total_records,
            'total_hours': round(total_hours, 1),
            'total_amount': '***' if is_time_keeper else round(total_amount, 2),
            'unique_workers': unique_workers,
            'pending_count': pending_count,
            'approved_count': approved_count,
        },
        'max_id': max_id,
        'server_time': timezone.now().isoformat()
    })


@login_required
def api_overtime_employee_lookup(request):
    """Look up employee details by QR code text or last digits of Emp ID."""
    q = request.GET.get('q', '').strip()
    if not q:
        return JsonResponse({'status': 'error', 'message': 'No search query provided'}, status=400)

    # 1. Try exact emp_id match
    emp = Employee.objects.filter(emp_id__iexact=q).first()
    
    # 2. Try partial/last digits match or CID match
    if not emp:
        emp = Employee.objects.filter(
            Q(emp_id__icontains=q) |
            Q(cid_number__icontains=q) |
            Q(name__icontains=q) |
            Q(contact_info__icontains=q)
        ).first()

    if not emp:
        # Check bundled employee_rates_data.json for instant resolution
        import json
        clean_q = q.strip()
        matched_info = None
        json_path = os.path.join(settings.BASE_DIR, 'portal', 'employee_rates_data.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as fp:
                    rates_map = json.load(fp)
                matched_info = rates_map.get(clean_q)
                if not matched_info:
                    for k, v in rates_map.items():
                        if k.endswith(clean_q) or clean_q in k:
                            matched_info = v
                            break
            except Exception:
                pass

        if matched_info:
            emp, _ = Employee.objects.get_or_create(
                emp_id=matched_info['emp_id'],
                defaults={
                    'name': matched_info['name'],
                    'designation': matched_info['designation'],
                    'department': matched_info['department'],
                    'overtime_rate': matched_info['overtime_rate'],
                    'status': 'Active'
                }
            )
        elif len(clean_q) >= 4:
            # Fallback auto initialization
            return JsonResponse({
                'status': 'success',
                'is_new': True,
                'employee': {
                    'id': None,
                    'emp_id': clean_q,
                    'name': f'Employee {clean_q}',
                    'designation': 'Worker / Operator',
                    'department': 'Plant & Machinery Operations - RVJ',
                    'cid_number': '-',
                    'account_number': '-',
                    'blood_group': 'O+',
                    'contact_info': '-',
                    'overtime_rate': 120.0,
                    'status': 'Active',
                },
                'message': f'✨ New card detected ({clean_q})! Auto-initialized.'
            })
        else:
            return JsonResponse({'status': 'not_found', 'message': f'Employee with ID/keyword "{q}" not found in database.'})

    # Default overtime rate based on designation if not set
    rate = emp.overtime_rate or 0.0
    if rate == 0.0:
        desig_lower = (emp.designation or '').lower()
        if 'driver' in desig_lower or 'operator' in desig_lower:
            rate = 120.0
        elif 'supervisor' in desig_lower or 'foreman' in desig_lower:
            rate = 150.0
        elif 'mechanic' in desig_lower or 'electrician' in desig_lower:
            rate = 140.0
        else:
            rate = 100.0

    is_time_keeper = (request.user.system_role == 'TIME_KEEPER')

    return JsonResponse({
        'status': 'success',
        'employee': {
            'id': emp.id,
            'emp_id': emp.emp_id or q,
            'name': emp.name or 'Unknown Employee',
            'designation': emp.designation or 'Worker',
            'department': emp.department or 'Operations',
            'cid_number': '-' if is_time_keeper else (emp.cid_number or getattr(emp, 'passport_details', '') or '-'),
            'account_number': '-' if is_time_keeper else (emp.account_number or '-'),
            'blood_group': getattr(emp, 'blood_group', '-') or '-',
            'contact_info': emp.contact_info or '-',
            'overtime_rate': 0.0 if is_time_keeper else rate,
            'status': emp.status or 'Active',
        }
    })


@login_required
def api_overtime_submit_punch(request):
    """Saves a punch IN, punch OUT, or complete overtime record."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    import json
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    emp_id = str(data.get('emp_id', '')).strip()
    if not emp_id:
        return JsonResponse({'status': 'error', 'message': 'Employee ID is required'}, status=400)

    emp_name = str(data.get('employee_name', '')).strip()
    designation = str(data.get('designation', 'Worker')).strip()
    department = str(data.get('department', 'Operations')).strip()
    cid_number = str(data.get('cid_number', '')).strip()
    account_number = str(data.get('account_number', '')).strip()
    contact_info = str(data.get('contact_info', '')).strip()
    
    try:
        rate = float(data.get('overtime_rate', 0.0) or 0.0)
    except ValueError:
        rate = 0.0

    date_str = data.get('date', '').strip() or timezone.localdate().strftime('%Y-%m-%d')
    shift = data.get('shift', 'Day').strip()
    punch_type = data.get('punch_type', 'IN').strip()
    location_zone = data.get('location_zone', 'Zone 1 & 2').strip()
    in_time = data.get('in_time', '').strip() or localtime().strftime('%I:%M %p')
    out_time = data.get('out_time', '').strip()
    
    try:
        ot_hours = float(data.get('overtime_hours', 0.0) or 0.0)
    except ValueError:
        ot_hours = 0.0

    # If in_time and out_time provided and ot_hours is 0, auto compute difference
    if in_time and out_time and ot_hours == 0.0:
        try:
            t1 = datetime.datetime.strptime(in_time, '%I:%M %p')
            t2 = datetime.datetime.strptime(out_time, '%I:%M %p')
            diff = (t2 - t1).total_seconds() / 3600.0
            if diff < 0: diff += 24.0 # Crosses midnight
            ot_hours = round(diff, 1)
        except Exception:
            pass

    ot_amount = round(ot_hours * rate, 2)
    work_description = str(data.get('work_description', '')).strip()
    vehicle_regn = str(data.get('vehicle_regn', '')).strip()
    remarks = str(data.get('remarks', '')).strip()

    # Link Employee object
    emp_obj = Employee.objects.filter(emp_id__iexact=emp_id).first()
    if emp_obj:
        if account_number and account_number != '-' and (not emp_obj.account_number or emp_obj.account_number == '-'):
            emp_obj.account_number = account_number
            emp_obj.save(update_fields=['account_number'])
        if cid_number and cid_number != '-' and (not emp_obj.cid_number or emp_obj.cid_number == '-'):
            emp_obj.cid_number = cid_number
            emp_obj.save(update_fields=['cid_number'])

    # Resolve official Overtime Rate
    if rate <= 0.0:
        if emp_obj and emp_obj.overtime_rate and emp_obj.overtime_rate > 0:
            rate = emp_obj.overtime_rate
        else:
            desig_lower = (designation or (emp_obj.designation if emp_obj else '')).lower()
            if 'driver' in desig_lower or 'operator' in desig_lower:
                rate = 120.0
            elif 'supervisor' in desig_lower or 'foreman' in desig_lower:
                rate = 150.0
            elif 'mechanic' in desig_lower or 'electrician' in desig_lower:
                rate = 140.0
            else:
                rate = 100.0

    # Resolve official Account Number & CID Number
    real_account_number = ''
    if account_number and account_number != '-':
        real_account_number = account_number
    elif emp_obj and emp_obj.account_number and emp_obj.account_number != '-':
        real_account_number = emp_obj.account_number

    real_cid_number = ''
    if cid_number and cid_number != '-':
        real_cid_number = cid_number
    elif emp_obj and emp_obj.cid_number and emp_obj.cid_number != '-':
        real_cid_number = emp_obj.cid_number

    # Duplicate check for same date, shift and employee
    existing = OvertimeRecord.objects.filter(
        date=date_str, shift=shift, emp_id_snapshot__iexact=emp_id
    ).first()

    if punch_type == 'IN':
        if existing:
            # Employee ALREADY punched IN today! Return friendly notice without changing to OUT
            return JsonResponse({
                'status': 'success',
                'action': 'already_punched_in',
                'is_duplicate': True,
                'message': f"⚠️ {existing.employee_name} already Punched IN at {existing.in_time} today ({existing.location_zone}).",
                'record': {
                    'id': existing.id,
                    'emp_id': existing.emp_id_snapshot,
                    'name': existing.employee_name,
                    'zone': existing.location_zone,
                    'punch_type': existing.punch_type,
                    'hours': existing.overtime_hours,
                    'amount': existing.overtime_amount,
                    'status': existing.status,
                    'in_time': existing.in_time,
                    'out_time': existing.out_time
                },
                'record_id': existing.id
            })
        else:
            # Fresh Punch IN
            in_time_val = in_time or localtime().strftime('%I:%M %p')
            record = OvertimeRecord.objects.create(
                employee=emp_obj,
                emp_id_snapshot=emp_id,
                employee_name=emp_name or (emp_obj.name if emp_obj else emp_id),
                designation=designation or (emp_obj.designation if emp_obj else 'Worker'),
                department=department or (emp_obj.department if emp_obj else 'Operations'),
                cid_number=real_cid_number,
                account_number=real_account_number,
                contact_info=contact_info or (emp_obj.contact_info if emp_obj else ''),
                overtime_rate=rate,
                date=date_str,
                shift=shift,
                location_zone=location_zone,
                punch_type='IN',
                in_time=in_time_val,
                out_time='',
                overtime_hours=0.0,
                overtime_amount=0.0,
                work_description=work_description,
                vehicle_regn=vehicle_regn,
                time_keeper=request.user,
                time_keeper_name=request.user.full_name or request.user.username,
                status='Pending',
                remarks=remarks
            )

            log_activity(
                user=request.user,
                action_type='CREATE',
                module_name='Overtime Management',
                description=f"Logged Punch IN for {record.employee_name} ({emp_id}) at {location_zone} [{shift} Shift]."
            )

            return JsonResponse({
                'status': 'success',
                'action': 'punched_in',
                'message': f"✅ Punch IN recorded for {record.employee_name} at {record.in_time}!",
                'record': {
                    'id': record.id,
                    'emp_id': record.emp_id_snapshot,
                    'name': record.employee_name,
                    'zone': record.location_zone,
                    'punch_type': 'IN',
                    'hours': 0.0,
                    'amount': 0.0,
                    'status': record.status,
                    'in_time': record.in_time,
                    'out_time': ''
                },
                'record_id': record.id
            })

    elif punch_type == 'OUT':
        out_time_val = out_time or localtime().strftime('%I:%M %p')
        if existing:
            # Complete Punch OUT for today's entry
            existing.out_time = out_time_val
            existing.punch_type = 'OUT'
            if existing.overtime_rate <= 0:
                existing.overtime_rate = rate
            if not existing.account_number or existing.account_number == '-':
                existing.account_number = real_account_number
            if not existing.cid_number or existing.cid_number == '-':
                existing.cid_number = real_cid_number

            # Accurately compute duration between in_time and out_time
            calc_diff = parse_time_diff_hours(existing.in_time, existing.out_time)
            if calc_diff > 0:
                existing.overtime_hours = calc_diff
            elif ot_hours > 0:
                existing.overtime_hours = round(ot_hours, 2)
            else:
                existing.overtime_hours = 0.0
            
            existing.overtime_amount = round(existing.overtime_hours * existing.overtime_rate, 2)
            if work_description:
                existing.work_description = work_description
            existing.save()

            log_activity(
                user=request.user,
                action_type='UPDATE',
                module_name='Overtime Management',
                description=f"Punched OUT for {emp_name} ({emp_id}) at {existing.location_zone} - {existing.overtime_hours} hrs (Nu. {existing.overtime_amount})."
            )

            return JsonResponse({
                'status': 'success',
                'action': 'punched_out',
                'message': f"✅ Punch OUT recorded for {emp_name}! Total OT: {existing.overtime_hours} Hours (Nu. {existing.overtime_amount})",
                'record': {
                    'id': existing.id,
                    'emp_id': existing.emp_id_snapshot,
                    'name': existing.employee_name,
                    'zone': existing.location_zone,
                    'punch_type': 'OUT',
                    'hours': existing.overtime_hours,
                    'amount': existing.overtime_amount,
                    'status': existing.status,
                    'in_time': existing.in_time,
                    'out_time': existing.out_time
                },
                'record_id': existing.id
            })
        else:
            # Standalone Punch OUT without prior IN
            in_time_val = in_time or '08:00 AM'
            calc_diff = parse_time_diff_hours(in_time_val, out_time_val)
            if calc_diff > 0:
                ot_hours = calc_diff
            elif ot_hours <= 0.0:
                ot_hours = 0.0
            ot_amount = round(ot_hours * rate, 2)

            record = OvertimeRecord.objects.create(
                employee=emp_obj,
                emp_id_snapshot=emp_id,
                employee_name=emp_name or (emp_obj.name if emp_obj else emp_id),
                designation=designation or (emp_obj.designation if emp_obj else 'Worker'),
                department=department or (emp_obj.department if emp_obj else 'Operations'),
                cid_number=cid_number or (emp_obj.cid_number if emp_obj else ''),
                account_number=account_number or (emp_obj.account_number if emp_obj else ''),
                contact_info=contact_info or (emp_obj.contact_info if emp_obj else ''),
                overtime_rate=rate,
                date=date_str,
                shift=shift,
                location_zone=location_zone,
                punch_type='OUT',
                in_time=in_time_val,
                out_time=out_time_val,
                overtime_hours=ot_hours,
                overtime_amount=ot_amount,
                work_description=work_description,
                vehicle_regn=vehicle_regn,
                time_keeper=request.user,
                time_keeper_name=request.user.full_name or request.user.username,
                status='Pending',
                remarks=remarks
            )

            log_activity(
                user=request.user,
                action_type='CREATE',
                module_name='Overtime Management',
                description=f"Logged Standalone Punch OUT for {record.employee_name} ({emp_id}) - {record.overtime_hours} hrs."
            )

            return JsonResponse({
                'status': 'success',
                'action': 'punched_out',
                'message': f"✅ Punch OUT logged for {record.employee_name}! Total OT: {record.overtime_hours} Hours (Nu. {record.overtime_amount})",
                'record': {
                    'id': record.id,
                    'emp_id': record.emp_id_snapshot,
                    'name': record.employee_name,
                    'zone': record.location_zone,
                    'punch_type': 'OUT',
                    'hours': record.overtime_hours,
                    'amount': record.overtime_amount,
                    'status': record.status,
                    'in_time': record.in_time,
                    'out_time': record.out_time
                },
                'record_id': record.id
            })

    # Create new Overtime Record
    record = OvertimeRecord.objects.create(
        employee=emp_obj,
        emp_id_snapshot=emp_id,
        employee_name=emp_name or (emp_obj.name if emp_obj else emp_id),
        designation=designation or (emp_obj.designation if emp_obj else 'Worker'),
        department=department or (emp_obj.department if emp_obj else 'Operations'),
        cid_number=cid_number or (emp_obj.cid_number if emp_obj else ''),
        account_number=account_number or (emp_obj.account_number if emp_obj else ''),
        contact_info=contact_info or (emp_obj.contact_info if emp_obj else ''),
        overtime_rate=rate,
        date=date_str,
        shift=shift,
        location_zone=location_zone,
        punch_type=punch_type,
        in_time=in_time,
        out_time=out_time,
        overtime_hours=ot_hours,
        overtime_amount=ot_amount,
        work_description=work_description,
        vehicle_regn=vehicle_regn,
        time_keeper=request.user,
        time_keeper_name=request.user.full_name or request.user.username,
        status='Pending',
        remarks=remarks
    )

    # Log Activity
    log_activity(
        user=request.user,
        action_type='CREATE',
        module_name='Overtime Management',
        description=f"Logged Overtime ({punch_type}) for {record.employee_name} ({emp_id}) at {location_zone} [{shift} Shift]."
    )

    return JsonResponse({
        'status': 'success',
        'action': 'created',
        'message': f"Overtime entry logged successfully for {record.employee_name} ({emp_id})!",
        'record': {
            'id': record.id,
            'emp_id': record.emp_id_snapshot,
            'name': record.employee_name,
            'designation': record.designation,
            'zone': record.location_zone,
            'shift': record.shift,
            'in_time': record.in_time,
            'out_time': record.out_time or '-',
            'hours': record.overtime_hours,
            'amount': record.overtime_amount,
        }
    })


@login_required
def api_overtime_approve(request, record_id):
    """Approve or Reject an Overtime record by Project Manager / Authorized Admin."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized: You do not have permission to approve or reject overtime records.'}, status=403)

    record = get_object_or_404(OvertimeRecord, id=record_id)
    action = request.POST.get('action', 'Approve').strip()

    if action == 'Approve':
        record.status = 'Approved'
        record.approved_by = request.user
        record.approved_at = timezone.now()
        record.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
        
        log_activity(
            user=request.user,
            action_type='APPROVE',
            module_name='Overtime Management',
            description=f"Approved Overtime for {record.employee_name} ({record.emp_id_snapshot}) - {record.overtime_hours} hrs (Nu. {record.overtime_amount})."
        )
        return JsonResponse({'status': 'success', 'message': f'Record for {record.employee_name} Approved!'})
    else:
        record.status = 'Rejected'
        record.approved_by = request.user
        record.approved_at = timezone.now()
        record.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
        
        log_activity(
            user=request.user,
            action_type='UPDATE',
            module_name='Overtime Management',
            description=f"Rejected Overtime for {record.employee_name} ({record.emp_id_snapshot})."
        )
        return JsonResponse({'status': 'success', 'message': f'Record for {record.employee_name} Rejected.'})


@login_required
def api_overtime_bulk_approve(request):
    """Bulk approves multiple overtime records selected by authorized users."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized: You do not have permission to approve overtime records.'}, status=403)

    import json
    try:
        data = json.loads(request.body.decode('utf-8'))
        ids = data.get('ids', [])
    except Exception:
        ids = request.POST.getlist('ids')

    if not ids:
        return JsonResponse({'status': 'error', 'message': 'No records selected'}, status=400)

    updated_count = OvertimeRecord.objects.filter(id__in=ids).update(
        status='Approved',
        approved_by=request.user,
        approved_at=timezone.now()
    )

    log_activity(
        user=request.user,
        action_type='APPROVE',
        module_name='Overtime Management',
        description=f"Bulk approved {updated_count} overtime records."
    )

    return JsonResponse({'status': 'success', 'message': f'Successfully approved {updated_count} overtime records!'})


@login_required
def export_overtime_excel(request):
    """Export Overtime & Payroll data to Excel matching the exact Official PDF Report format."""
    if request.user.system_role == 'TIME_KEEPER':
        return HttpResponse('Unauthorized: Time Keepers cannot download financial payroll data.', status=403)

    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return HttpResponse('Unauthorized: You do not have permission to export Overtime data.', status=403)

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image as XLImage
    from django.conf import settings
    import os

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    status = request.GET.get('status', 'All').strip()
    
    report_title = request.GET.get('report_title', 'Sunday Night Emergency Patrolling Team').strip() or 'Sunday Night Emergency Patrolling Team'
    custom_sign_label = request.GET.get('custom_sign_label', 'Checked By').strip() or 'Checked By'
    custom_sign_name = request.GET.get('custom_sign_name', 'Dorji Wangchuk').strip() or 'Dorji Wangchuk'
    custom_sign_desig = request.GET.get('custom_sign_desig', 'Site Supervisor').strip() or 'Site Supervisor'
    prepared_by_name = request.GET.get('prepared_by_name', 'Sonam Lhamo').strip() or 'Sonam Lhamo'
    prepared_by_desig = request.GET.get('prepared_by_desig', 'HR Assistant').strip() or 'HR Assistant'
    verified_by_name = request.GET.get('verified_by_name', 'Jasthola').strip() or 'Jasthola'
    verified_by_desig = request.GET.get('verified_by_desig', 'Head HR & Admin').strip() or 'Head HR & Admin'
    approved_by_name = request.GET.get('approved_by_name', 'Karma Tshering').strip() or 'Karma Tshering'
    approved_by_desig = request.GET.get('approved_by_desig', 'Project Manager').strip() or 'Project Manager'

    qs = OvertimeRecord.objects.all().order_by('-date', '-created_at')
    if date_from: qs = qs.filter(date__gte=date_from)
    if date_to: qs = qs.filter(date__lte=date_to)
    if shift and shift != 'All': qs = qs.filter(shift=shift)
    if zone and zone != 'All': qs = qs.filter(location_zone=zone)
    if status and status != 'All': qs = qs.filter(status=status)

    today = timezone.localdate()
    today_formatted = f"{today.day}.{today.month}.{today.year}"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Overtime Report"

    # Borders & Fills
    thin_black = Border(
        left=Side(style='thin', color='000000'),
        right=Side(style='thin', color='000000'),
        top=Side(style='thin', color='000000'),
        bottom=Side(style='thin', color='000000')
    )

    # 1. Company Letterhead Header Row (Row 1)
    ws.row_dimensions[1].height = 46

    # Embed Company Diamond Logo (Left - A1)
    logo_path = os.path.join(settings.BASE_DIR, 'media', 'branding', 'rigsar_vajra_logo.png')
    if os.path.exists(logo_path):
        try:
            img_logo = XLImage(logo_path)
            img_logo.width = 48
            img_logo.height = 48
            ws.add_image(img_logo, 'A1')
        except Exception:
            pass

    # Embed ISO Certified Badge (Right - H1)
    iso_path = os.path.join(settings.BASE_DIR, 'media', 'branding', 'iso_certified_logo.png')
    if os.path.exists(iso_path):
        try:
            img_iso = XLImage(iso_path)
            img_iso.width = 48
            img_iso.height = 48
            ws.add_image(img_iso, 'H1')
        except Exception:
            pass

    ws.merge_cells('A1:H1')
    cell_h1 = ws['A1']
    cell_h1.value = "RIGSAR - VAJRA JV"
    cell_h1.font = Font(name='Segoe UI', size=16, bold=True, color='000000')
    cell_h1.alignment = Alignment(horizontal='center', vertical='center')

    # 2. Golden Accent Line (Row 2)
    gold_fill = PatternFill(start_color='F59E0B', end_color='F59E0B', fill_type='solid')
    for col_idx in range(1, 9):
        ws.cell(row=2, column=col_idx).fill = gold_fill
    ws.row_dimensions[2].height = 4

    # 3. Date Header (Row 3)
    ws.merge_cells('E3:H3')
    cell_h3 = ws['E3']
    cell_h3.value = f"Date: {today_formatted}"
    cell_h3.font = Font(name='Times New Roman', size=11)
    cell_h3.alignment = Alignment(horizontal='right', vertical='center')
    ws.row_dimensions[3].height = 18

    # 4. Report Header Title Box (Row 4)
    ws.merge_cells('A4:H4')
    cell_h4 = ws['A4']
    cell_h4.value = report_title
    cell_h4.font = Font(name='Times New Roman', size=11, bold=True)
    cell_h4.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[4].height = 22
    for col_idx in range(1, 9):
        ws.cell(row=4, column=col_idx).border = thin_black

    # 5. Table Column Headers (Row 5)
    headers = ['SN', 'Emp ID', 'Name', 'Designation', 'Acc No', 'OT Rate', 'OT hrs', 'OT Amt']
    ws.append(headers)
    ws.row_dimensions[5].height = 20

    for col_idx in range(1, 9):
        cell = ws.cell(row=5, column=col_idx)
        cell.font = Font(name='Times New Roman', size=10, bold=True)
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_black

    # 6. Data Rows
    total_ot_hours = 0.0
    total_ot_amount = 0.0

    for idx, r in enumerate(qs, 1):
        acct_val = r.account_number or (r.employee.account_number if r.employee else '') or '-'
        hrs_val = round(float(r.overtime_hours or 0.0), 1)
        rate_val = round(float(r.overtime_rate or 0.0), 2)
        amt_val = round(float(r.overtime_amount or 0.0), 2)

        total_ot_hours += hrs_val
        total_ot_amount += amt_val

        row_data = [
            idx,
            r.emp_id_snapshot,
            r.employee_name,
            r.designation,
            str(acct_val),
            rate_val,
            hrs_val,
            amt_val,
        ]
        ws.append(row_data)
        current_row = 5 + idx
        ws.row_dimensions[current_row].height = 19
        for col_idx in range(1, 9):
            cell = ws.cell(row=current_row, column=col_idx)
            cell.border = thin_black
            cell.font = Font(name='Times New Roman', size=10)
            if col_idx in [1, 5, 6, 7, 8]:
                cell.alignment = Alignment(horizontal='center', vertical='center')
            else:
                cell.alignment = Alignment(horizontal='left', vertical='center')

    # 7. Total Summary Row
    total_row_idx = 5 + len(qs) + 1
    ws.merge_cells(start_row=total_row_idx, start_column=1, end_row=total_row_idx, end_column=6)
    tot_label = ws.cell(row=total_row_idx, column=1)
    tot_label.value = "Total"
    tot_label.font = Font(name='Times New Roman', size=10, bold=True)
    tot_label.alignment = Alignment(horizontal='center', vertical='center')

    tot_hrs = ws.cell(row=total_row_idx, column=7)
    tot_hrs.value = round(total_ot_hours, 1)
    tot_hrs.font = Font(name='Times New Roman', size=10, bold=True)
    tot_hrs.alignment = Alignment(horizontal='center', vertical='center')

    tot_amt = ws.cell(row=total_row_idx, column=8)
    tot_amt.value = round(total_ot_amount, 2)
    tot_amt.font = Font(name='Times New Roman', size=10, bold=True)
    tot_amt.alignment = Alignment(horizontal='center', vertical='center')

    ws.row_dimensions[total_row_idx].height = 20
    for col_idx in range(1, 9):
        ws.cell(row=total_row_idx, column=col_idx).border = thin_black

    # 8. Signatory 4-Column Footer matching exact PDF layout
    sign_row_1 = total_row_idx + 3
    sign_row_2 = sign_row_1 + 3

    sign_blocks = [
        (1, 2, f"{custom_sign_label}: {custom_sign_name}", custom_sign_desig),
        (3, 4, f"Prepared By: {prepared_by_name}", prepared_by_desig),
        (5, 6, f"Verified By: {verified_by_name}", verified_by_desig),
        (7, 8, f"Approved By: {approved_by_name}", approved_by_desig),
    ]

    for start_c, end_c, title_text, desig_text in sign_blocks:
        ws.merge_cells(start_row=sign_row_1, start_column=start_c, end_row=sign_row_1, end_column=end_c)
        c_title = ws.cell(row=sign_row_1, column=start_c)
        c_title.value = title_text
        c_title.font = Font(name='Times New Roman', size=10.5)
        c_title.alignment = Alignment(horizontal='center', vertical='center')

        ws.merge_cells(start_row=sign_row_2, start_column=start_c, end_row=sign_row_2, end_column=end_c)
        c_desig = ws.cell(row=sign_row_2, column=start_c)
        c_desig.value = desig_text
        c_desig.font = Font(name='Times New Roman', size=10.5)
        c_desig.alignment = Alignment(horizontal='center', vertical='center')

    ws.row_dimensions[sign_row_1].height = 18
    ws.row_dimensions[sign_row_2].height = 18

    # Set precise column widths matching document
    col_widths = {'A': 6, 'B': 20, 'C': 22, 'D': 18, 'E': 16, 'F': 12, 'G': 10, 'H': 12}
    for col_let, w in col_widths.items():
        ws.column_dimensions[col_let].width = w

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Overtime_Report_{today_formatted}.xlsx"'
    wb.save(response)
    return response


@login_required
def export_overtime_pdf(request):
    """Printable PDF report for Project Manager overtime reviews."""
    if request.user.system_role == 'TIME_KEEPER':
        return HttpResponse('Unauthorized: Time Keepers cannot download financial payroll reports.', status=403)

    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return HttpResponse('Unauthorized: You do not have permission to export Overtime PDF.', status=403)

    today = timezone.localdate()
    today_formatted = f"{today.day}.{today.month}.{today.year}"

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    status = request.GET.get('status', 'All').strip()

    qs = OvertimeRecord.objects.all().order_by('-date', '-created_at')
    if date_from: qs = qs.filter(date__gte=date_from)
    elif not date_to: qs = qs.filter(date=today)
    if date_to: qs = qs.filter(date__lte=date_to)
    if shift and shift != 'All': qs = qs.filter(shift=shift)
    if zone and zone != 'All': qs = qs.filter(location_zone=zone)
    if status and status != 'All': qs = qs.filter(status=status)

    total_records = qs.count()
    total_hours = sum(r.overtime_hours for r in qs)
    total_amount = sum(r.overtime_amount for r in qs)

    filter_desc_parts = []
    if date_from: filter_desc_parts.append(f"From: {date_from}")
    if date_to: filter_desc_parts.append(f"To: {date_to}")
    if shift and shift != 'All': filter_desc_parts.append(f"Shift: {shift}")
    if zone and zone != 'All': filter_desc_parts.append(f"Zone: {zone}")
    if status and status != 'All': filter_desc_parts.append(f"Status: {status}")
    filter_desc = " | ".join(filter_desc_parts) if filter_desc_parts else f"Date: {today}"

    report_title = request.GET.get('report_title', 'Sunday Night Emergency Patrolling Team').strip() or 'Sunday Night Emergency Patrolling Team'
    custom_sign_label = request.GET.get('custom_sign_label', 'Checked By').strip() or 'Checked By'
    custom_sign_name = request.GET.get('custom_sign_name', 'Dorji Wangchuk').strip() or 'Dorji Wangchuk'
    custom_sign_desig = request.GET.get('custom_sign_desig', 'Site Supervisor').strip() or 'Site Supervisor'
    prepared_by_name = request.GET.get('prepared_by_name', 'Sonam Lhamo').strip() or 'Sonam Lhamo'
    prepared_by_desig = request.GET.get('prepared_by_desig', 'HR Assistant').strip() or 'HR Assistant'
    verified_by_name = request.GET.get('verified_by_name', 'Jasthola').strip() or 'Jasthola'
    verified_by_desig = request.GET.get('verified_by_desig', 'Head HR & Admin').strip() or 'Head HR & Admin'
    approved_by_name = request.GET.get('approved_by_name', 'Karma Tshering').strip() or 'Karma Tshering'
    approved_by_desig = request.GET.get('approved_by_desig', 'Project Manager').strip() or 'Project Manager'

    context = {
        'records': qs[:500],
        'total_records': total_records,
        'total_hours': round(total_hours, 1),
        'total_amount': round(total_amount, 2),
        'filter_desc': filter_desc,
        'today_formatted': today_formatted,
        'report_generated_at': timezone.now(),
        'report_title': report_title,
        'custom_sign_label': custom_sign_label,
        'custom_sign_name': custom_sign_name,
        'custom_sign_desig': custom_sign_desig,
        'prepared_by_name': prepared_by_name,
        'prepared_by_desig': prepared_by_desig,
        'verified_by_name': verified_by_name,
        'verified_by_desig': verified_by_desig,
        'approved_by_name': approved_by_name,
        'approved_by_desig': approved_by_desig,
    }
    return render(request, 'overtime_pdf.html', context)


@login_required
def api_overtime_get_record(request, record_id):
    """Retrieve full overtime record details for editing modal."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized: You do not have permission to view record details.'}, status=403)

    record = get_object_or_404(OvertimeRecord, id=record_id)
    return JsonResponse({
        'status': 'success',
        'record': {
            'id': record.id,
            'date': record.date.strftime('%Y-%m-%d') if record.date else '',
            'emp_id': record.emp_id_snapshot,
            'name': record.employee_name,
            'designation': record.designation,
            'department': record.department,
            'cid_number': record.cid_number or (record.employee.cid_number if record.employee else '') or '',
            'account_number': record.account_number or (record.employee.account_number if record.employee else '') or '',
            'shift': record.shift,
            'location_zone': record.location_zone,
            'in_time': record.in_time or '',
            'out_time': record.out_time or '',
            'overtime_hours': record.overtime_hours,
            'overtime_rate': record.overtime_rate,
            'overtime_amount': record.overtime_amount,
            'work_description': record.work_description or '',
            'status': record.status,
            'remarks': record.remarks or '',
            'time_keeper_name': record.time_keeper_name or 'Self',
        }
    })


@login_required
def api_overtime_update_record(request, record_id):
    """Update overtime record fields by authorized Manager or Superuser."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized: You do not have permission to edit records.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    record = get_object_or_404(OvertimeRecord, id=record_id)

    import json
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    # Update basic fields
    if 'employee_name' in data and str(data['employee_name']).strip():
        record.employee_name = str(data['employee_name']).strip()
    if 'designation' in data:
        record.designation = str(data['designation']).strip()
    if 'department' in data:
        record.department = str(data['department']).strip()
    if 'account_number' in data:
        record.account_number = str(data['account_number']).strip()
        if record.employee and not record.employee.account_number:
            record.employee.account_number = record.account_number
            record.employee.save(update_fields=['account_number'])
    if 'cid_number' in data:
        record.cid_number = str(data['cid_number']).strip()
        if record.employee and not record.employee.cid_number:
            record.employee.cid_number = record.cid_number
            record.employee.save(update_fields=['cid_number'])
    if 'date' in data and str(data['date']).strip():
        record.date = str(data['date']).strip()
    if 'shift' in data and str(data['shift']).strip():
        record.shift = str(data['shift']).strip()
    if 'location_zone' in data and str(data['location_zone']).strip():
        record.location_zone = str(data['location_zone']).strip()
    if 'in_time' in data:
        record.in_time = str(data['in_time']).strip()
    if 'out_time' in data:
        record.out_time = str(data['out_time']).strip()
    if 'work_description' in data:
        record.work_description = str(data['work_description']).strip()
    if 'remarks' in data:
        record.remarks = str(data['remarks']).strip()
    if 'status' in data and str(data['status']).strip():
        record.status = str(data['status']).strip()
        if record.status == 'Approved' and not record.approved_by:
            record.approved_by = request.user
            record.approved_at = timezone.now()

    # Rate and Hours update
    try:
        if 'overtime_rate' in data and data['overtime_rate'] != '':
            record.overtime_rate = float(data['overtime_rate'] or 0.0)
    except ValueError:
        pass

    try:
        if 'overtime_hours' in data and str(data['overtime_hours']).strip() != '':
            record.overtime_hours = float(data['overtime_hours'] or 0.0)
        elif record.in_time and record.out_time:
            record.overtime_hours = parse_time_diff_hours(record.in_time, record.out_time)
    except ValueError:
        pass

    record.overtime_amount = round(record.overtime_hours * record.overtime_rate, 2)
    record.save()

    # Log activity
    log_activity(
        user=request.user,
        action_type='UPDATE',
        module_name='Overtime Management',
        description=f"Updated Overtime Record for {record.employee_name} ({record.emp_id_snapshot}) - {record.overtime_hours} hrs [Status: {record.status}]."
    )

    return JsonResponse({
        'status': 'success',
        'message': f'Record for {record.employee_name} updated successfully!',
        'record': {
            'id': record.id,
            'name': record.employee_name,
            'emp_id': record.emp_id_snapshot,
            'designation': record.designation,
            'department': record.department,
            'shift': record.shift,
            'zone': record.location_zone,
            'in_time': record.in_time or '-',
            'out_time': record.out_time or '-',
            'hours': record.overtime_hours,
            'rate': record.overtime_rate,
            'amount': record.overtime_amount,
            'status': record.status,
            'date': record.date.strftime('%d %b %Y') if record.date else '-',
        }
    })


@login_required
def api_overtime_delete_record(request, record_id):
    """Delete an Overtime record (Authorized Admin / Superuser only)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({
            'status': 'error', 
            'message': 'Unauthorized: You do not have permission to delete records.'
        }, status=403)

    record = get_object_or_404(OvertimeRecord, id=record_id)
        
    emp_name = record.employee_name
    emp_id = record.emp_id_snapshot
    date_str = str(record.date)

    record.delete()

    log_activity(
        user=request.user,
        action_type='DELETE',
        module_name='Overtime Management',
        description=f"Deleted Overtime Record of {emp_name} ({emp_id}) dated {date_str}."
    )

    return JsonResponse({'status': 'success', 'message': f'Overtime record of {emp_name} deleted successfully.'})


@login_required
def api_overtime_bulk_delete(request):
    """Bulk delete selected overtime records (Authorized Manager / Superuser only)."""
    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized: You do not have permission to delete records.'}, status=403)

    import json
    try:
        data = json.loads(request.body.decode('utf-8'))
        ids = data.get('ids', [])
    except Exception:
        ids = request.POST.getlist('ids')

    if not ids:
        return JsonResponse({'status': 'error', 'message': 'No records selected for deletion.'}, status=400)

    deleted_count, _ = OvertimeRecord.objects.filter(id__in=ids).delete()

    log_activity(
        user=request.user,
        action_type='DELETE',
        module_name='Overtime Management',
        description=f"Bulk deleted {deleted_count} overtime records."
    )

    return JsonResponse({'status': 'success', 'message': f'Successfully deleted {deleted_count} overtime records!'})


@login_required
def overtime_export_hub_view(request):
    """Dedicated Overtime Export Hub with Advance Date Range & Field Filters."""
    if request.user.system_role == 'TIME_KEEPER':
        messages.error(request, "Permission denied: Time Keepers cannot access Overtime Export Hub.")
        return redirect('overtime_dashboard')

    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        messages.error(request, "Permission denied: You do not have permission to access Overtime Export Hub.")
        return redirect('dashboard')

    today = timezone.localdate()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    status = request.GET.get('status', 'All').strip()
    q = request.GET.get('q', '').strip()
    time_keeper_id = request.GET.get('time_keeper', '').strip()

    qs = OvertimeRecord.objects.all().order_by('-date', '-created_at')

    # Apply date filters
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    if shift and shift != 'All':
        qs = qs.filter(shift=shift)
    if zone and zone != 'All':
        qs = qs.filter(location_zone=zone)
    if status and status != 'All':
        qs = qs.filter(status=status)
    if time_keeper_id and time_keeper_id != 'All':
        qs = qs.filter(time_keeper_id=time_keeper_id)

    if q:
        qs = qs.filter(
            Q(employee_name__icontains=q) |
            Q(emp_id_snapshot__icontains=q) |
            Q(designation__icontains=q) |
            Q(department__icontains=q) |
            Q(work_description__icontains=q)
        )

    # Aggregates
    total_records = qs.count()
    total_hours = round(sum(r.overtime_hours for r in qs), 1)
    total_amount = round(sum(r.overtime_amount for r in qs), 2)
    unique_workers = qs.values('emp_id_snapshot').distinct().count()
    pending_count = qs.filter(status='Pending').count()
    approved_count = qs.filter(status='Approved').count()

    # Dropdown choices
    zones = ["Zone 1 & 2", "Zone 3 & 4", "Zone 5 & 6", "Workshop", "Yard / Batching Plant", "Crusher Unit", "Office / Camp Area", "Site Security"]
    shifts = ["Day", "Night", "General"]
    statuses = ["Pending", "Approved", "Rejected"]
    time_keepers = User.objects.filter(
        Q(system_role='TIME_KEEPER') |
        Q(assigned_modules__icontains='overtime_management')
    ).filter(is_active=True).distinct().order_by('full_name', 'username')

    context = {
        'records': qs[:150],
        'total_records': total_records,
        'total_hours': total_hours,
        'total_amount': total_amount,
        'unique_workers': unique_workers,
        'pending_count': pending_count,
        'approved_count': approved_count,
        'date_from': date_from,
        'date_to': date_to,
        'shift': shift,
        'zone': zone,
        'status': status,
        'q': q,
        'time_keeper_id': time_keeper_id,
        'zones': zones,
        'shifts': shifts,
        'statuses': statuses,
        'time_keepers': time_keepers,
        'today': today,
    }
    return render(request, 'overtime_export.html', context)


@login_required
def export_overtime_csv(request):
    """Export Overtime & Payroll data to CSV."""
    if request.user.system_role == 'TIME_KEEPER':
        return HttpResponse('Unauthorized: Time Keepers cannot download financial payroll data.', status=403)

    if not user_has_overtime_permission(request.user, is_timekeeper_scanner=False):
        return HttpResponse('Unauthorized: You do not have permission to export Overtime CSV.', status=403)

    import csv

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    shift = request.GET.get('shift', 'All').strip()
    zone = request.GET.get('zone', 'All').strip()
    status = request.GET.get('status', 'All').strip()
    q = request.GET.get('q', '').strip()

    qs = OvertimeRecord.objects.all().order_by('-date', '-created_at')
    if date_from: qs = qs.filter(date__gte=date_from)
    if date_to: qs = qs.filter(date__lte=date_to)
    if shift and shift != 'All': qs = qs.filter(shift=shift)
    if zone and zone != 'All': qs = qs.filter(location_zone=zone)
    if status and status != 'All': qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(employee_name__icontains=q) |
            Q(emp_id_snapshot__icontains=q) |
            Q(designation__icontains=q)
        )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="Overtime_Payroll_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
    writer = csv.writer(response)

    writer.writerow([
        'SR NO', 'DATE', 'EMP ID', 'FULL NAME', 'DESIGNATION', 'DEPARTMENT',
        'CID NUMBER', 'ACCOUNT NUMBER', 'SHIFT', 'LOCATION / ZONE', 'IN TIME', 'OUT TIME',
        'OT HOURS', 'HOURLY RATE (Nu. )', 'TOTAL AMOUNT (Nu. )', 'WORK DESCRIPTION', 'TIME KEEPER', 'STATUS'
    ])

    for idx, r in enumerate(qs, 1):
        acct_val = r.account_number or (r.employee.account_number if r.employee else '') or '-'
        cid_val = r.cid_number or (r.employee.cid_number if r.employee else '') or '-'
        writer.writerow([
            idx,
            str(r.date),
            r.emp_id_snapshot,
            r.employee_name,
            r.designation,
            r.department,
            cid_val,
            acct_val,
            r.shift,
            r.location_zone,
            r.in_time or '-',
            r.out_time or '-',
            r.overtime_hours,
            r.overtime_rate,
            r.overtime_amount,
            r.work_description or '-',
            r.time_keeper_name or '-',
            r.status,
        ])

    return response


@login_required
def download_system_backup(request):
    """
    Generates and downloads a 100% portable, complete system backup ZIP archive.
    Suitable for shifting the entire website to ANY new hosting provider
    (PythonAnywhere, VPS, AWS, Hostinger, DigitalOcean, cPanel, or Localhost).
    """
    can_backup = request.user.is_superuser or request.user.system_role == 'MANAGER' or getattr(request.user, 'can_view_user_activity', False)
    if not can_backup:
        messages.error(request, "Permission denied: Only Managers and Admins can download system backups.")
        return redirect('dashboard')

    import zipfile, io, os, datetime
    from django.conf import settings
    from django.http import HttpResponse
    from django.core.management import call_command

    timestamp_str = datetime.datetime.now().strftime('%Y_%m_%d_%H%M%S')
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. Active Database File (db.sqlite3)
        db_file = os.path.join(settings.BASE_DIR, 'db.sqlite3')
        if os.path.exists(db_file):
            zf.write(db_file, arcname='db.sqlite3')

        # 2. Universal JSON Database Fixture (Works on PostgreSQL, MySQL, SQLite on any hosting)
        try:
            fixture_out = io.StringIO()
            call_command(
                'dumpdata',
                '--natural-foreign',
                '--natural-primary',
                '--indent', '2',
                exclude=['contenttypes', 'auth.Permission', 'sessions'],
                stdout=fixture_out
            )
            zf.writestr('database_dump_fixture.json', fixture_out.getvalue().encode('utf-8'))
        except Exception as fe:
            zf.writestr('database_dump_error.txt', f"Note: Could not generate fixture: {fe}")

        # 3. Master Employee Rates & Catalog JSON
        json_file = os.path.join(settings.BASE_DIR, 'portal', 'employee_rates_data.json')
        if os.path.exists(json_file):
            zf.write(json_file, arcname='portal/employee_rates_data.json')

        # 4. Uploaded Media & Documents (media/)
        media_dir = getattr(settings, 'MEDIA_ROOT', None)
        if media_dir and os.path.exists(media_dir):
            for root, dirs, files in os.walk(media_dir):
                for f in files:
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, media_dir)
                    zf.write(full_p, arcname=os.path.join('media', rel_p))

        # 5. Hosting Migration & Restore Guide
        migration_guide = f"""========================================================================
P&M PORTAL - COMPLETE SYSTEM BACKUP & HOSTING MIGRATION GUIDE
Created on: {datetime.datetime.now().strftime('%d %B %Y, %I:%M %p')}
Generated by: {request.user.full_name or request.user.username} ({request.user.system_role})
========================================================================

HOW TO RESTORE & SHIFT THIS WEBSITE TO ANY NEW HOSTING / SERVER:

OPTION A: DIRECT RESTORE (Easiest - using db.sqlite3)
------------------------------------------------------------------------
1. Copy the website source code to your new hosting / server.
2. Replace or place 'db.sqlite3' from this ZIP into your project root folder.
3. Extract the 'media/' folder into your project root directory.
4. Place 'portal/employee_rates_data.json' inside the 'portal/' folder.
5. Run migrations to verify schema:
       python manage.py migrate
6. Start the server / reload Web App:
       python manage.py runserver  (or Reload in cPanel/PythonAnywhere/Gunicorn)

OPTION B: MIGRATING TO POSTGRESQL / MYSQL ON CLOUD (AWS, DigitalOcean, VPS)
------------------------------------------------------------------------
1. Set up your PostgreSQL/MySQL database credentials in 'core_project/settings.py'.
2. Run migrations to create clean tables:
       python manage.py migrate
3. Load the complete universal database dump:
       python manage.py loaddata database_dump_fixture.json
4. Copy the 'media/' folder to your server's media path.
5. All users, passwords, vehicle allocations, form entries, and overtime logs 
   will be 100% restored and fully active!

========================================================================
Backup Contents:
- db.sqlite3: Raw SQLite database
- database_dump_fixture.json: Universal JSON dump (cross-database portable)
- media/: All uploaded employee photos, vehicle RC copies, insurance PDFs
- portal/employee_rates_data.json: Master employee rates and designations
========================================================================
"""
        zf.writestr('README_MIGRATION_GUIDE.txt', migration_guide)

    zip_buffer.seek(0)
    filename = f"Complete_Website_Full_Backup_{timestamp_str}.zip"
    response = HttpResponse(zip_buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ============================================================================
# P&M DEPARTMENT ATTENDANCE MANAGEMENT SYSTEM VIEWS & API ENDPOINTS
# ============================================================================

def _get_pnm_employees_queryset(dept_filter='pnm'):
    pnm_q = (
        Q(department__icontains='Plant') |
        Q(department__icontains='P&M') |
        Q(department__icontains='Machinery') |
        Q(department__icontains='Workshop') |
        Q(department__icontains='Maintenance') |
        Q(department__icontains='Scania') |
        Q(department__icontains='Fabrication')
    )
    qs = Employee.objects.filter(pnm_q)
    if dept_filter and dept_filter not in ['all', 'pnm', '']:
        qs = qs.filter(department=dept_filter)
    return qs.order_by('name')


@login_required
def attendance_hub_view(request):
    """
    Main Attendance Register & Management Hub for P&M Department
    """
    import calendar
    today_date = timezone.localdate()
    
    date_str = request.GET.get('date', '').strip()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today_date
    except Exception:
        selected_date = today_date
        
    dept_filter = request.GET.get('dept', 'pnm').strip()
    shift_filter = request.GET.get('shift', 'all').strip()
    status_filter = request.GET.get('status', 'all').strip()
    search_query = request.GET.get('q', '').strip().lower()

    # Get P&M Employees strictly
    employees_qs = _get_pnm_employees_queryset(dept_filter)
    
    # Get all distinct P&M sub-departments for dropdown
    pnm_base_q = (
        Q(department__icontains='Plant') |
        Q(department__icontains='P&M') |
        Q(department__icontains='Machinery') |
        Q(department__icontains='Workshop') |
        Q(department__icontains='Maintenance') |
        Q(department__icontains='Scania') |
        Q(department__icontains='Fabrication')
    )
    all_departments = sorted(list(
        Employee.objects.filter(pnm_base_q)
        .exclude(department__isnull=True)
        .exclude(department='')
        .values_list('department', flat=True)
        .distinct()
    ))
    all_designations = sorted(list(
        Employee.objects.filter(pnm_base_q)
        .exclude(designation__isnull=True)
        .exclude(designation='')
        .values_list('designation', flat=True)
        .distinct()
    ))

    # Fetch attendance records for selected date
    attendances = EmployeeAttendance.objects.filter(date=selected_date).select_related('employee', 'marked_by')
    att_map = {att.employee_id: att for att in attendances}

    # Build employee attendance list
    emp_attendance_list = []
    
    present_cnt = 0
    absent_cnt = 0
    leave_cnt = 0
    half_day_cnt = 0
    holiday_cnt = 0
    not_marked_cnt = 0
    
    shift_morning_cnt = 0
    shift_night_cnt = 0

    for emp in employees_qs:
        rec = att_map.get(emp.id)
        current_status = rec.status if rec else 'Not Marked'
        raw_shift = rec.shift if rec else (emp.current_shift or 'Morning Shift (07:00 AM - 07:00 PM)')
        
        # Standardize to 2 shifts: Morning Shift or Night Shift
        if 'night' in raw_shift.lower() or 'shift c' in raw_shift.lower() or 'shift b' in raw_shift.lower():
            current_shift = 'Night Shift (07:00 PM - 07:00 AM)'
            shift_night_cnt += 1
        else:
            current_shift = 'Morning Shift (07:00 AM - 07:00 PM)'
            shift_morning_cnt += 1

        in_time_str = rec.in_time.strftime('%I:%M %p') if (rec and rec.in_time) else ''
        out_time_str = rec.out_time.strftime('%I:%M %p') if (rec and rec.out_time) else ''
        remarks = rec.remarks if rec else ''
        punch_source = rec.punch_source if rec else 'MANUAL'

        # Count statistics for P&M
        if current_status == 'Present': present_cnt += 1
        elif current_status == 'Absent': absent_cnt += 1
        elif current_status == 'On Leave': leave_cnt += 1
        elif current_status == 'Half Day': half_day_cnt += 1
        elif current_status == 'Holiday': holiday_cnt += 1
        else: not_marked_cnt += 1

        searchable = f"{emp.name or ''} {emp.emp_id or ''} {emp.designation or ''} {emp.department or ''} {emp.nationality or ''} {emp.contractor_agency or ''} {emp.cid_number or ''} {remarks or ''}".lower()

        emp_attendance_list.append({
            'employee': emp,
            'attendance': rec,
            'status': current_status,
            'shift': current_shift,
            'in_time': in_time_str,
            'out_time': out_time_str,
            'remarks': remarks,
            'punch_source': punch_source,
            'searchable': searchable,
            'visible': True,
        })

    total_employees_count = len(employees_qs)
    present_total = present_cnt + (half_day_cnt * 0.5)
    attendance_pct = round((present_total / total_employees_count * 100), 1) if total_employees_count > 0 else 0

    context = {
        'selected_date': selected_date,
        'selected_date_str': selected_date.strftime('%Y-%m-%d'),
        'today_str': today_date.strftime('%Y-%m-%d'),
        'dept_filter': dept_filter,
        'shift_filter': shift_filter,
        'status_filter': status_filter,
        'search_query': search_query,
        'all_departments': all_departments,
        'all_designations': all_designations,
        'emp_attendance_list': emp_attendance_list,
        'stats': {
            'total_employees': total_employees_count,
            'present_count': present_cnt,
            'absent_count': absent_cnt,
            'leave_count': leave_cnt,
            'half_day_count': half_day_cnt,
            'holiday_count': holiday_cnt,
            'not_marked_count': not_marked_cnt,
            'attendance_pct': attendance_pct,
            'shift_morning': shift_morning_cnt,
            'shift_night': shift_night_cnt,
        }
    }
    return render(request, 'attendance_hub.html', context)


@login_required
def attendance_scanner_view(request):
    """
    Live QR / Barcode Scanner for P&M Attendance (identical fast scanner as Overtime)
    """
    today_date = timezone.localdate()
    
    # Recent punches for today
    recent_punches = EmployeeAttendance.objects.filter(
        date=today_date,
        punch_source='QR_SCAN'
    ).select_related('employee').order_by('-updated_at')[:20]

    context = {
        'today': today_date,
        'today_str': today_date.strftime('%d %b %Y'),
        'recent_punches': recent_punches,
    }
    return render(request, 'attendance_scan.html', context)


@login_required
def api_attendance_submit_punch(request):
    """
    API endpoint for QR / Barcode Scanner to punch attendance
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid HTTP method'}, status=405)

    raw_qr = request.POST.get('qr_data') or request.POST.get('emp_code') or request.POST.get('emp_id') or ''
    punch_mode = request.POST.get('punch_mode', 'AUTO').upper() # IN, OUT, AUTO
    shift = request.POST.get('shift', '').strip()
    
    raw_qr = raw_qr.strip()
    if not raw_qr:
        return JsonResponse({'success': False, 'message': 'No QR code or Employee ID provided'}, status=400)

    # Parse QR content (supports JSON, 'HR-EMP-...', CID, plain ID)
    emp_query = raw_qr
    if raw_qr.startswith('{') and raw_qr.endswith('}'):
        try:
            data = json.loads(raw_qr)
            emp_query = data.get('emp_id') or data.get('id') or data.get('cid') or raw_qr
        except Exception:
            pass

    emp = Employee.objects.filter(
        Q(emp_id__iexact=emp_query) |
        Q(cid_number__iexact=emp_query) |
        Q(passport_details__iexact=emp_query) |
        Q(work_permit_no__iexact=emp_query) |
        Q(name__iexact=emp_query)
    ).first()

    if not emp:
        return JsonResponse({'success': False, 'message': f'Employee not found for code: "{emp_query}"'}, status=404)

    pnm_keywords = ['plant', 'p&m', 'machinery', 'workshop', 'maintenance', 'scania', 'fabrication']
    is_pnm = any(k in (emp.department or '').lower() for k in pnm_keywords)
    if not is_pnm:
        return JsonResponse({
            'success': False,
            'message': f'{emp.name} ({emp.emp_id}) belongs to "{emp.department or "Non-P&M"}". Attendance Hub is strictly for P&M Department employees.'
        }, status=400)

    today = timezone.localdate()
    now_time = timezone.localtime().time()

    att, created = EmployeeAttendance.objects.get_or_create(
        employee=emp,
        date=today,
        defaults={
            'shift': shift or emp.current_shift or 'General',
            'status': 'Present',
            'punch_source': 'QR_SCAN',
            'marked_by': request.user
        }
    )

    action_type = "IN"
    if punch_mode == 'IN':
        att.in_time = now_time
        att.status = 'Present'
        action_type = "IN"
    elif punch_mode == 'OUT':
        att.out_time = now_time
        att.status = 'Present'
        action_type = "OUT"
    else: # AUTO
        if not att.in_time:
            att.in_time = now_time
            att.status = 'Present'
            action_type = "IN"
        elif not att.out_time:
            att.out_time = now_time
            att.status = 'Present'
            action_type = "OUT"
        else:
            att.out_time = now_time
            action_type = "OUT"

    if shift:
        att.shift = shift
    att.punch_source = 'QR_SCAN'
    att.marked_by = request.user
    att.save()

    return JsonResponse({
        'success': True,
        'action_type': action_type,
        'message': f"Punch {action_type} Recorded for {emp.name}",
        'employee': {
            'id': emp.id,
            'emp_id': emp.emp_id,
            'name': emp.name,
            'designation': emp.designation,
            'department': emp.department,
            'nationality': emp.nationality,
            'shift': att.shift,
            'status': att.status,
            'in_time': att.in_time.strftime('%I:%M %p') if att.in_time else '-',
            'out_time': att.out_time.strftime('%I:%M %p') if att.out_time else '-',
            'photo_url': emp.document_upload.url if emp.document_upload else ''
        }
    })


@login_required
def api_attendance_mark_single(request):
    """
    AJAX update single employee attendance
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)

    emp_id = request.POST.get('emp_id')
    date_str = request.POST.get('date')
    status = request.POST.get('status', 'Present')
    shift = request.POST.get('shift')
    remarks = request.POST.get('remarks', '')
    in_time_str = request.POST.get('in_time', '').strip()
    out_time_str = request.POST.get('out_time', '').strip()

    emp = get_object_or_404(Employee, id=emp_id)
    try:
        att_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.localdate()
    except Exception:
        att_date = timezone.localdate()

    in_time_obj = None
    out_time_obj = None
    if in_time_str:
        for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p', '%I:%M%p'):
            try:
                in_time_obj = datetime.strptime(in_time_str, fmt).time()
                break
            except ValueError:
                pass
    if out_time_str:
        for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p', '%I:%M%p'):
            try:
                out_time_obj = datetime.strptime(out_time_str, fmt).time()
                break
            except ValueError:
                pass

    att, created = EmployeeAttendance.objects.get_or_create(
        employee=emp,
        date=att_date,
        defaults={
            'shift': shift or emp.current_shift or 'General',
            'status': status,
            'punch_source': 'MANUAL',
            'marked_by': request.user
        }
    )

    att.status = status
    if shift:
        att.shift = shift
    if in_time_obj is not None or not in_time_str:
        att.in_time = in_time_obj
    if out_time_obj is not None or not out_time_str:
        att.out_time = out_time_obj
    att.remarks = remarks
    att.marked_by = request.user
    att.save()

    return JsonResponse({
        'success': True,
        'message': f"Attendance updated for {emp.name} ({status})",
        'status': att.status,
        'shift': att.shift
    })


@login_required
def api_attendance_bulk_mark(request):
    """
    Bulk mark attendance for all or filtered employees
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)

    date_str = request.POST.get('date')
    dept = request.POST.get('dept', 'pnm')
    status = request.POST.get('status', 'Present')
    shift = request.POST.get('shift', '')
    emp_ids = request.POST.getlist('emp_ids[]') or request.POST.getlist('emp_ids')

    try:
        att_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.localdate()
    except Exception:
        att_date = timezone.localdate()

    if emp_ids:
        employees = Employee.objects.filter(id__in=emp_ids)
    else:
        employees = _get_pnm_employees_queryset(dept)

    updated_count = 0
    for emp in employees:
        att, created = EmployeeAttendance.objects.get_or_create(
            employee=emp,
            date=att_date,
            defaults={
                'shift': shift or emp.current_shift or 'General',
                'status': status,
                'punch_source': 'BULK',
                'marked_by': request.user
            }
        )
        att.status = status
        if shift:
            att.shift = shift
        att.punch_source = 'BULK'
        att.marked_by = request.user
        att.save()
        updated_count += 1

    return JsonResponse({
        'success': True,
        'message': f"Successfully marked {updated_count} employees as {status} for {att_date.strftime('%d %b %Y')}!",
        'updated_count': updated_count
    })


@login_required
def api_attendance_assign_shift(request):
    """
    Assign/update shifts for employees
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)

    emp_ids = request.POST.getlist('emp_ids[]') or request.POST.getlist('emp_ids')
    single_emp_id = request.POST.get('emp_id')
    new_shift = request.POST.get('shift', 'General').strip()
    date_str = request.POST.get('date')

    if single_emp_id and not emp_ids:
        emp_ids = [single_emp_id]

    if not emp_ids:
        return JsonResponse({'success': False, 'message': 'No employees selected'}, status=400)

    try:
        att_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else timezone.localdate()
    except Exception:
        att_date = timezone.localdate()

    employees = Employee.objects.filter(id__in=emp_ids)
    for emp in employees:
        emp.current_shift = new_shift
        emp.save(update_fields=['current_shift'])

        # Also update today's attendance record if exists
        att = EmployeeAttendance.objects.filter(employee=emp, date=att_date).first()
        if att:
            att.shift = new_shift
            att.save(update_fields=['shift'])

    return JsonResponse({
        'success': True,
        'message': f"Shift updated to '{new_shift}' for {len(employees)} employee(s)!",
        'shift': new_shift
    })


@login_required
def attendance_muster_roll_view(request):
    """
    Monthly Muster Roll Report Matrix (Days 1 to 31) for P&M Department
    """
    import calendar
    today = timezone.localdate()
    
    try:
        month = int(request.GET.get('month', today.month))
        year = int(request.GET.get('year', today.year))
    except Exception:
        month = today.month
        year = today.year

    dept_filter = request.GET.get('dept', 'pnm')
    shift_filter = request.GET.get('shift', 'all')
    
    num_days = calendar.monthrange(year, month)[1]
    days_list = list(range(1, num_days + 1))
    
    month_name = calendar.month_name[month]
    
    employees_qs = _get_pnm_employees_queryset(dept_filter)
    if shift_filter != 'all':
        employees_qs = employees_qs.filter(current_shift__icontains=shift_filter)

    # Fetch all attendances for this month
    start_date = datetime(year, month, 1).date()
    end_date = datetime(year, month, num_days).date()
    
    attendances = EmployeeAttendance.objects.filter(
        date__gte=start_date,
        date__lte=end_date,
        employee__in=employees_qs
    ).select_related('employee')

    # Map (emp_id, day) -> record
    att_matrix = {}
    for att in attendances:
        att_matrix[(att.employee_id, att.date.day)] = att

    muster_data = []
    total_p_sum = 0
    total_a_sum = 0
    total_l_sum = 0
    total_hd_sum = 0

    for emp in employees_qs:
        daily_records = []
        p_cnt = 0
        a_cnt = 0
        l_cnt = 0
        hd_cnt = 0
        h_cnt = 0

        for day in days_list:
            cur_d = datetime(year, month, day).date()
            is_sunday = cur_d.weekday() == 6
            rec = att_matrix.get((emp.id, day))
            
            if rec:
                code = 'P' if rec.status == 'Present' else ('A' if rec.status == 'Absent' else ('L' if rec.status == 'On Leave' else ('HD' if rec.status == 'Half Day' else 'H')))
                if rec.status == 'Present': p_cnt += 1
                elif rec.status == 'Absent': a_cnt += 1
                elif rec.status == 'On Leave': l_cnt += 1
                elif rec.status == 'Half Day': hd_cnt += 1
                elif rec.status == 'Holiday': h_cnt += 1
            else:
                code = 'WO' if is_sunday else '-'
                if is_sunday: h_cnt += 1

            daily_records.append({
                'day': day,
                'code': code,
                'is_sunday': is_sunday,
                'record': rec
            })

        total_present_days = p_cnt + (hd_cnt * 0.5)
        total_p_sum += p_cnt
        total_a_sum += a_cnt
        total_l_sum += l_cnt
        total_hd_sum += hd_cnt

        muster_data.append({
            'employee': emp,
            'days': daily_records,
            'present_count': p_cnt,
            'absent_count': a_cnt,
            'leave_count': l_cnt,
            'half_day_count': hd_cnt,
            'holiday_count': h_cnt,
            'total_present_days': total_present_days,
        })

    pnm_base_q = (
        Q(department__icontains='Plant') |
        Q(department__icontains='P&M') |
        Q(department__icontains='Machinery') |
        Q(department__icontains='Workshop') |
        Q(department__icontains='Maintenance') |
        Q(department__icontains='Scania') |
        Q(department__icontains='Fabrication')
    )
    all_departments = sorted(list(
        Employee.objects.filter(pnm_base_q)
        .exclude(department__isnull=True)
        .exclude(department='')
        .values_list('department', flat=True)
        .distinct()
    ))

    context = {
        'month': month,
        'year': year,
        'month_name': month_name,
        'days_list': days_list,
        'num_days': num_days,
        'dept_filter': dept_filter,
        'shift_filter': shift_filter,
        'all_departments': all_departments,
        'muster_data': muster_data,
        'months_range': [(i, calendar.month_name[i]) for i in range(1, 13)],
        'years_range': list(range(today.year - 2, today.year + 3)),
        'total_employees': len(employees_qs),
        'total_p_sum': total_p_sum,
        'total_a_sum': total_a_sum,
        'total_l_sum': total_l_sum,
    }
    return render(request, 'attendance_muster_roll.html', context)


@login_required
def export_attendance_daily_excel(request):
    """
    Export Daily Attendance Register to Excel with Rigsar-Vajra JV letterhead & branding.
    Supports custom title, designation filter, status filter, and selective column inclusion.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    date_str = request.GET.get('date', '').strip()
    today_date = timezone.localdate()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today_date
    except Exception:
        selected_date = today_date

    dept_filter = request.GET.get('dept', 'pnm').strip()
    shift_filter = request.GET.get('shift', 'all').strip()
    status_filter = request.GET.get('status', 'all').strip()
    desig_filter = (request.GET.get('designation') or request.GET.get('desig', 'all')).strip()
    title_param = (request.GET.get('title') or request.GET.get('report_title', '')).strip()
    cols_param = request.GET.get('cols', '').strip()
    
    employees_qs = _get_pnm_employees_queryset(dept_filter)
    if shift_filter != 'all':
        employees_qs = employees_qs.filter(current_shift__icontains=shift_filter)
    if desig_filter and desig_filter != 'all':
        employees_qs = employees_qs.filter(designation__icontains=desig_filter)

    attendances = EmployeeAttendance.objects.filter(date=selected_date).select_related('employee')
    att_map = {att.employee_id: att for att in attendances}

    # All available column definitions: (key, header_title, align, width)
    ALL_COL_DEFS = [
        ('SL.NO', 'SL. NO', 'center', 8),
        ('EMPLOYEE ID', 'EMPLOYEE ID', 'center', 18),
        ('NAME', 'EMPLOYEE NAME', 'left', 28),
        ('DESIGNATION', 'DESIGNATION', 'left', 24),
        ('DEPARTMENT', 'DEPARTMENT', 'left', 28),
        ('SHIFT', 'ASSIGNED SHIFT', 'center', 24),
        ('STATUS', 'ATTENDANCE STATUS', 'center', 18),
        ('IN TIME', 'IN TIME', 'center', 14),
        ('OUT TIME', 'OUT TIME', 'center', 14),
        ('REMARKS', 'REMARKS', 'left', 22),
        ('SIGNATURE', 'SIGNATURE', 'center', 18)
    ]

    if cols_param:
        raw_selected = [c.strip().upper() for c in cols_param.split(',') if c.strip()]
        norm_selected = set()
        for c in raw_selected:
            if c in ['SL', 'SL NO', 'SL.NO', 'SL. NO']: norm_selected.add('SL.NO')
            elif c in ['EMP ID', 'EMPLOYEE ID', 'EMPID']: norm_selected.add('EMPLOYEE ID')
            elif c in ['NAME', 'EMPLOYEE NAME', 'EMP NAME']: norm_selected.add('NAME')
            elif c in ['DESIGNATION', 'DESIG']: norm_selected.add('DESIGNATION')
            elif c in ['DEPARTMENT', 'DEPT']: norm_selected.add('DEPARTMENT')
            elif c in ['SHIFT', 'ASSIGNED SHIFT']: norm_selected.add('SHIFT')
            elif c in ['STATUS', 'ATTENDANCE STATUS']: norm_selected.add('STATUS')
            elif c in ['IN TIME', 'INTIME', 'IN']: norm_selected.add('IN TIME')
            elif c in ['OUT TIME', 'OUTTIME', 'OUT']: norm_selected.add('OUT TIME')
            elif c in ['REMARKS', 'REMARK']: norm_selected.add('REMARKS')
            elif c in ['SIGNATURE', 'SIGN']: norm_selected.add('SIGNATURE')
            else: norm_selected.add(c)
        active_cols = [col for col in ALL_COL_DEFS if col[0] in norm_selected]
        if not active_cols:
            active_cols = ALL_COL_DEFS
    else:
        active_cols = ALL_COL_DEFS

    total_cols = len(active_cols)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily Attendance"
    ws.views.sheetView[0].showGridLines = True

    # 1. Company Header
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
    c1 = ws.cell(row=1, column=1, value="RIGSAR - VAJRA JV")
    c1.font = Font(name='Segoe UI', size=16, bold=True, color='1E293B')
    c1.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 36

    # 2. Subtitle
    heading_text = title_param if title_param else f"P&M DEPARTMENT DAILY ATTENDANCE REGISTER - {selected_date.strftime('%d %B %Y').upper()}"
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=total_cols)
    c2 = ws.cell(row=2, column=1, value=heading_text)
    c2.font = Font(name='Segoe UI', size=11, bold=True, color='2563EB')
    c2.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 24

    header_fill = PatternFill(start_color='1E293B', end_color='1E293B', fill_type='solid')
    header_font = Font(name='Segoe UI', size=10, bold=True, color='FFFFFF')
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')
    
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    # 3. Table Header
    ws.row_dimensions[4].height = 28
    for col_idx, (col_key, header_title, align, width) in enumerate(active_cols, 1):
        cell = ws.cell(row=4, column=col_idx, value=header_title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width

    # Fills for status
    present_fill = PatternFill(start_color='DCFCE7', end_color='DCFCE7', fill_type='solid')
    absent_fill = PatternFill(start_color='FEE2E2', end_color='FEE2E2', fill_type='solid')
    leave_fill = PatternFill(start_color='FEF9C3', end_color='FEF9C3', fill_type='solid')
    halfday_fill = PatternFill(start_color='E0E7FF', end_color='E0E7FF', fill_type='solid')

    row_num = 5
    row_count = 0
    for emp in employees_qs:
        rec = att_map.get(emp.id)
        status = rec.status if rec else "Not Marked"
        shift_val = rec.shift if rec else (emp.current_shift or "General")
        in_t = rec.in_time.strftime('%I:%M %p') if (rec and rec.in_time) else "-"
        out_t = rec.out_time.strftime('%I:%M %p') if (rec and rec.out_time) else "-"
        remarks_val = rec.remarks if (rec and rec.remarks) else "-"

        if status_filter != 'all' and status_filter.lower() != status.lower():
            continue

        row_count += 1
        for col_idx, (col_key, header_title, align, width) in enumerate(active_cols, 1):
            if col_key == 'SL.NO': val = row_count
            elif col_key == 'EMPLOYEE ID': val = emp.emp_id or "-"
            elif col_key == 'NAME': val = emp.name or "-"
            elif col_key == 'DESIGNATION': val = emp.designation or "-"
            elif col_key == 'DEPARTMENT': val = emp.department or "-"
            elif col_key == 'SHIFT': val = shift_val
            elif col_key == 'STATUS': val = status
            elif col_key == 'IN TIME': val = in_t
            elif col_key == 'OUT TIME': val = out_t
            elif col_key == 'REMARKS': val = remarks_val
            elif col_key == 'SIGNATURE': val = ""
            else: val = "-"

            cell = ws.cell(row=row_num, column=col_idx, value=val)
            cell.alignment = left_align if align == 'left' else center_align
            cell.border = thin_border

            if col_key == 'STATUS':
                cell.font = Font(name='Segoe UI', size=10, bold=True)
                if status == 'Present': cell.fill = present_fill
                elif status == 'Absent': cell.fill = absent_fill
                elif status == 'On Leave': cell.fill = leave_fill
                elif status == 'Half Day': cell.fill = halfday_fill

        ws.row_dimensions[row_num].height = 22
        row_num += 1

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="PNM_Daily_Attendance_{selected_date.strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


@login_required
def export_attendance_muster_excel(request):
    """
    Export Monthly Muster Roll Matrix (Days 1 to 31) to Excel
    """
    import openpyxl, calendar
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    today = timezone.localdate()
    try:
        month = int(request.GET.get('month', today.month))
        year = int(request.GET.get('year', today.year))
    except Exception:
        month = today.month
        year = today.year

    dept_filter = request.GET.get('dept', 'pnm').strip()
    shift_filter = request.GET.get('shift', 'all').strip()
    desig_filter = (request.GET.get('designation') or request.GET.get('desig', 'all')).strip()
    title_param = (request.GET.get('title') or request.GET.get('report_title', '')).strip()
    
    num_days = calendar.monthrange(year, month)[1]
    days_list = list(range(1, num_days + 1))
    month_name = calendar.month_name[month]

    employees_qs = _get_pnm_employees_queryset(dept_filter)
    if shift_filter != 'all':
        employees_qs = employees_qs.filter(current_shift__icontains=shift_filter)
    if desig_filter and desig_filter != 'all':
        employees_qs = employees_qs.filter(designation__icontains=desig_filter)

    start_date = datetime(year, month, 1).date()
    end_date = datetime(year, month, num_days).date()
    
    attendances = EmployeeAttendance.objects.filter(
        date__gte=start_date,
        date__lte=end_date,
        employee__in=employees_qs
    ).select_related('employee')

    att_matrix = {(att.employee_id, att.date.day): att for att in attendances}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Muster Roll {month_name[:3]}_{year}"
    ws.views.sheetView[0].showGridLines = True

    total_cols = 5 + num_days + 5

    # 1. Company Header
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
    c1 = ws.cell(row=1, column=1, value="RIGSAR - VAJRA JV")
    c1.font = Font(name='Segoe UI', size=16, bold=True, color='1E293B')
    c1.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 34

    # 2. Subtitle
    heading_text = title_param if title_param else f"MONTHLY MUSTER ROLL ATTENDANCE SHEET - {month_name.upper()} {year} (P&M DEPARTMENT)"
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=total_cols)
    c2 = ws.cell(row=2, column=1, value=heading_text)
    c2.font = Font(name='Segoe UI', size=11, bold=True, color='2563EB')
    c2.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 24

    # 3. Table Headers
    header_fill = PatternFill(start_color='1E293B', end_color='1E293B', fill_type='solid')
    sunday_fill = PatternFill(start_color='F1F5F9', end_color='F1F5F9', fill_type='solid')
    header_font = Font(name='Segoe UI', size=9, bold=True, color='FFFFFF')
    center_align = Alignment(horizontal='center', vertical='center')
    thin_border = Border(left=Side(style='thin', color='CBD5E1'), right=Side(style='thin', color='CBD5E1'), top=Side(style='thin', color='CBD5E1'), bottom=Side(style='thin', color='CBD5E1'))

    fixed_headers = ["SL", "EMP ID", "NAME", "DESIGNATION", "SHIFT"]
    for i, h in enumerate(fixed_headers, 1):
        cell = ws.cell(row=4, column=i, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # Days 1..31
    for day_idx, d in enumerate(days_list, 6):
        cur_d = datetime(year, month, d).date()
        is_sun = cur_d.weekday() == 6
        cell = ws.cell(row=4, column=day_idx, value=d)
        cell.fill = PatternFill(start_color='DC2626' if is_sun else '334155', fill_type='solid')
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border
        ws.column_dimensions[openpyxl.utils.get_column_letter(day_idx)].width = 3.8

    # Summary headers
    summary_start = 6 + num_days
    sum_headers = ["P", "A", "L", "HD", "TOTAL DAYS"]
    for idx, sh in enumerate(sum_headers, summary_start):
        cell = ws.cell(row=4, column=idx, value=sh)
        cell.fill = PatternFill(start_color='0F766E', fill_type='solid')
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border
        ws.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = 7

    ws.row_dimensions[4].height = 26

    # Widths for fixed cols
    ws.column_dimensions['A'].width = 5
    ws.column_dimensions['B'].width = 15
    ws.column_dimensions['C'].width = 24
    ws.column_dimensions['D'].width = 22
    ws.column_dimensions['E'].width = 16

    p_fill = PatternFill(start_color='DCFCE7', fill_type='solid')
    a_fill = PatternFill(start_color='FEE2E2', fill_type='solid')
    l_fill = PatternFill(start_color='FEF9C3', fill_type='solid')

    row_num = 5
    for sl, emp in enumerate(employees_qs, 1):
        ws.cell(row=row_num, column=1, value=sl).alignment = center_align
        ws.cell(row=row_num, column=2, value=emp.emp_id or "-").alignment = center_align
        ws.cell(row=row_num, column=3, value=emp.name or "-").alignment = Alignment(horizontal='left', vertical='center')
        ws.cell(row=row_num, column=4, value=emp.designation or "-").alignment = Alignment(horizontal='left', vertical='center')
        ws.cell(row=row_num, column=5, value=emp.current_shift or "General").alignment = center_align

        p_cnt = 0
        a_cnt = 0
        l_cnt = 0
        hd_cnt = 0

        for day_idx, d in enumerate(days_list, 6):
            cur_d = datetime(year, month, d).date()
            is_sun = cur_d.weekday() == 6
            rec = att_matrix.get((emp.id, d))
            
            d_cell = ws.cell(row=row_num, column=day_idx)
            d_cell.alignment = center_align
            d_cell.font = Font(name='Segoe UI', size=9, bold=True)
            d_cell.border = thin_border

            if rec:
                if rec.status == 'Present':
                    d_cell.value = 'P'
                    d_cell.fill = p_fill
                    p_cnt += 1
                elif rec.status == 'Absent':
                    d_cell.value = 'A'
                    d_cell.fill = a_fill
                    a_cnt += 1
                elif rec.status == 'On Leave':
                    d_cell.value = 'L'
                    d_cell.fill = l_fill
                    l_cnt += 1
                elif rec.status == 'Half Day':
                    d_cell.value = 'HD'
                    d_cell.fill = l_fill
                    hd_cnt += 1
                else:
                    d_cell.value = 'H'
            else:
                d_cell.value = 'WO' if is_sun else '-'
                if is_sun: d_cell.fill = sunday_fill

        tot_present = p_cnt + (hd_cnt * 0.5)
        ws.cell(row=row_num, column=summary_start, value=p_cnt).alignment = center_align
        ws.cell(row=row_num, column=summary_start + 1, value=a_cnt).alignment = center_align
        ws.cell(row=row_num, column=summary_start + 2, value=l_cnt).alignment = center_align
        ws.cell(row=row_num, column=summary_start + 3, value=hd_cnt).alignment = center_align
        
        tot_cell = ws.cell(row=row_num, column=summary_start + 4, value=tot_present)
        tot_cell.alignment = center_align
        tot_cell.font = Font(name='Segoe UI', size=10, bold=True, color='0F766E')

        for col_i in range(1, total_cols + 1):
            ws.cell(row=row_num, column=col_i).border = thin_border

        ws.row_dimensions[row_num].height = 20
        row_num += 1

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="PNM_Muster_Roll_{month_name}_{year}.xlsx"'
    wb.save(response)
    return response


@login_required
def export_attendance_pdf(request):
    """
    Export printable PDF view for Attendance (Daily Register & Monthly Muster Roll)
    """
    import calendar
    today_date = timezone.localdate()
    mode = request.GET.get('mode', 'daily').strip()

    if mode == 'muster':
        try:
            month = int(request.GET.get('month', today_date.month))
            year = int(request.GET.get('year', today_date.year))
        except Exception:
            month = today_date.month
            year = today_date.year

        dept_filter = request.GET.get('dept', 'pnm').strip()
        shift_filter = request.GET.get('shift', 'all').strip()
        desig_filter = (request.GET.get('designation') or request.GET.get('desig', 'all')).strip()
        title_param = (request.GET.get('title') or request.GET.get('report_title', '')).strip()

        num_days = calendar.monthrange(year, month)[1]
        days_list = list(range(1, num_days + 1))
        month_name = calendar.month_name[month]

        employees_qs = _get_pnm_employees_queryset(dept_filter)
        if shift_filter != 'all':
            employees_qs = employees_qs.filter(current_shift__icontains=shift_filter)
        if desig_filter and desig_filter != 'all':
            employees_qs = employees_qs.filter(designation__icontains=desig_filter)

        start_date = datetime(year, month, 1).date()
        end_date = datetime(year, month, num_days).date()
        attendances = EmployeeAttendance.objects.filter(
            date__gte=start_date,
            date__lte=end_date,
            employee__in=employees_qs
        ).select_related('employee')

        att_matrix = {}
        for att in attendances:
            att_matrix[(att.employee_id, att.date.day)] = att

        muster_data = []
        for emp in employees_qs:
            daily_records = []
            p_cnt = 0
            a_cnt = 0
            l_cnt = 0
            hd_cnt = 0
            for day in days_list:
                cur_d = datetime(year, month, day).date()
                is_sunday = cur_d.weekday() == 6
                rec = att_matrix.get((emp.id, day))
                if rec:
                    code = 'P' if rec.status == 'Present' else ('A' if rec.status == 'Absent' else ('L' if rec.status == 'On Leave' else ('HD' if rec.status == 'Half Day' else 'H')))
                    if rec.status == 'Present': p_cnt += 1
                    elif rec.status == 'Absent': a_cnt += 1
                    elif rec.status == 'On Leave': l_cnt += 1
                    elif rec.status == 'Half Day': hd_cnt += 1
                else:
                    code = 'WO' if is_sunday else '-'

                daily_records.append({'day': day, 'code': code, 'is_sunday': is_sunday})

            muster_data.append({
                'employee': emp,
                'days': daily_records,
                'present_count': p_cnt,
                'absent_count': a_cnt,
                'leave_count': l_cnt,
                'half_day_count': hd_cnt,
                'total_present_days': p_cnt + (hd_cnt * 0.5)
            })

        context = {
            'month': month,
            'year': year,
            'month_name': month_name,
            'days_list': days_list,
            'num_days': num_days,
            'dept_filter': dept_filter,
            'shift_filter': shift_filter,
            'desig_filter': desig_filter,
            'custom_title': title_param,
            'muster_data': muster_data,
            'total_employees': len(employees_qs)
        }
        return render(request, 'attendance_muster_pdf.html', context)

    # Daily PDF
    date_str = request.GET.get('date', '').strip()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today_date
    except Exception:
        selected_date = today_date

    dept_filter = request.GET.get('dept', 'pnm').strip()
    shift_filter = request.GET.get('shift', 'all').strip()
    status_filter = request.GET.get('status', 'all').strip()
    desig_filter = (request.GET.get('designation') or request.GET.get('desig', 'all')).strip()
    title_param = (request.GET.get('title') or request.GET.get('report_title', '')).strip()
    cols_param = request.GET.get('cols', '').strip()
    search_query = request.GET.get('q', '').strip().lower()

    selected_cols = []
    if cols_param:
        raw_selected = [c.strip().upper() for c in cols_param.split(',') if c.strip()]
        for c in raw_selected:
            if c in ['SL', 'SL NO', 'SL.NO', 'SL. NO']: selected_cols.append('SL.NO')
            elif c in ['EMP ID', 'EMPLOYEE ID', 'EMPID']: selected_cols.append('EMPLOYEE ID')
            elif c in ['NAME', 'EMPLOYEE NAME', 'EMP NAME']: selected_cols.append('NAME')
            elif c in ['DESIGNATION', 'DESIG']: selected_cols.append('DESIGNATION')
            elif c in ['DEPARTMENT', 'DEPT']: selected_cols.append('DEPARTMENT')
            elif c in ['SHIFT', 'ASSIGNED SHIFT']: selected_cols.append('SHIFT')
            elif c in ['STATUS', 'ATTENDANCE STATUS']: selected_cols.append('STATUS')
            elif c in ['IN TIME', 'INTIME', 'IN']: selected_cols.append('IN TIME')
            elif c in ['OUT TIME', 'OUTTIME', 'OUT']: selected_cols.append('OUT TIME')
            elif c in ['REMARKS', 'REMARK']: selected_cols.append('REMARKS')
            elif c in ['SIGNATURE', 'SIGN']: selected_cols.append('SIGNATURE')
            else: selected_cols.append(c)

    employees_qs = _get_pnm_employees_queryset(dept_filter)
    if desig_filter and desig_filter != 'all':
        employees_qs = employees_qs.filter(designation__icontains=desig_filter)

    attendances = EmployeeAttendance.objects.filter(date=selected_date).select_related('employee')
    att_map = {att.employee_id: att for att in attendances}

    emp_attendance_list = []
    present_cnt = 0
    absent_cnt = 0
    leave_cnt = 0
    half_day_cnt = 0

    for emp in employees_qs:
        rec = att_map.get(emp.id)
        current_status = rec.status if rec else 'Not Marked'
        current_shift = rec.shift if rec else (emp.current_shift or 'General')
        in_time_str = rec.in_time.strftime('%I:%M %p') if (rec and rec.in_time) else ''
        out_time_str = rec.out_time.strftime('%I:%M %p') if (rec and rec.out_time) else ''
        remarks_str = rec.remarks if (rec and rec.remarks) else ''

        if current_status == 'Present': present_cnt += 1
        elif current_status == 'Absent': absent_cnt += 1
        elif current_status == 'On Leave': leave_cnt += 1
        elif current_status == 'Half Day': half_day_cnt += 1

        if shift_filter != 'all' and shift_filter.lower() not in current_shift.lower():
            continue
        if status_filter != 'all' and status_filter.lower() != current_status.lower():
            continue
        if search_query:
            searchable = f"{emp.name} {emp.emp_id} {emp.designation} {emp.department}".lower()
            if search_query not in searchable:
                continue

        emp_attendance_list.append({
            'employee': emp,
            'status': current_status,
            'shift': current_shift,
            'in_time': in_time_str,
            'out_time': out_time_str,
            'remarks': remarks_str,
        })

    tot_emp = len(employees_qs)
    pres_tot = present_cnt + (half_day_cnt * 0.5)
    att_pct = round((pres_tot / tot_emp * 100), 1) if tot_emp > 0 else 0

    context = {
        'selected_date': selected_date,
        'selected_date_str': selected_date.strftime('%Y-%m-%d'),
        'dept_filter': dept_filter,
        'shift_filter': shift_filter,
        'status_filter': status_filter,
        'desig_filter': desig_filter,
        'custom_title': title_param,
        'selected_cols': selected_cols,
        'emp_attendance_list': emp_attendance_list,
        'stats': {
            'total_employees': tot_emp,
            'present_count': present_cnt,
            'absent_count': absent_cnt,
            'leave_count': leave_cnt,
            'half_day_count': half_day_cnt,
            'attendance_pct': att_pct
        }
    }
    return render(request, 'attendance_daily_pdf.html', context)








@csrf_exempt
@login_required
def api_admin_update_user_access(request):
    if not (request.user.is_superuser or request.user.system_role == 'MANAGER'):
        return JsonResponse({'status': 'error', 'message': 'Permission denied: Admin access required.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body)
        target_user_id = data.get('user_id')
        action = data.get('action')

        if not target_user_id:
            return JsonResponse({'status': 'error', 'message': 'User ID is required.'}, status=400)

        user = User.objects.get(id=target_user_id)

        if action == 'approve':
            new_role = data.get('role', 'DEO')
            user.system_role = new_role
            user.is_active = True
            user.save()
            log_activity(request.user, 'APPROVE', 'User Management', f"Approved user '{user.username}' with role '{new_role}'.", request)
            return JsonResponse({'status': 'success', 'message': f"User '{user.username}' approved as {new_role}."})

        elif action == 'toggle_active':
            user.is_active = not user.is_active
            user.save()
            st = "activated" if user.is_active else "blocked/suspended"
            log_activity(request.user, 'UPDATE', 'User Management', f"Account '{user.username}' was {st}.", request)
            return JsonResponse({'status': 'success', 'message': f"User '{user.username}' is now {st}.", 'is_active': user.is_active})

        elif action == 'update_role':
            new_role = data.get('role')
            if new_role:
                user.system_role = new_role
                if 'captain_category' in data:
                    user.captain_category = data.get('captain_category', 'All')
                user.save()
                log_activity(request.user, 'UPDATE', 'User Management', f"Updated role of '{user.username}' to '{new_role}'.", request)
                return JsonResponse({'status': 'success', 'message': f"Role updated for '{user.username}' to {new_role}."})

        elif action == 'update_modules':
            modules = data.get('assigned_modules', [])
            user.assigned_modules = modules
            if 'can_view_user_activity' in data:
                user.can_view_user_activity = bool(data.get('can_view_user_activity'))
            user.save()
            log_activity(request.user, 'UPDATE', 'User Management', f"Updated module permissions for '{user.username}'.", request)
            return JsonResponse({'status': 'success', 'message': f"Module permissions updated for '{user.username}'."})

        elif action == 'reset_password':
            new_pass = data.get('password')
            if new_pass:
                user.set_password(new_pass)
                user.otp = None
                user.save()
                log_activity(request.user, 'UPDATE', 'User Management', f"Reset password for user '{user.username}'.", request)
                return JsonResponse({'status': 'success', 'message': f"Password reset successfully for '{user.username}'."})

        return JsonResponse({'status': 'error', 'message': 'Invalid action parameter.'}, status=400)

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@csrf_exempt
@login_required
def api_admin_create_user(request):
    if not (request.user.is_superuser or request.user.system_role == 'MANAGER'):
        return JsonResponse({'status': 'error', 'message': 'Permission denied: Admin access required.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body)
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        full_name = data.get('full_name', '').strip()
        email = data.get('email', '').strip()
        phone_number = data.get('phone_number', '').strip()
        role = data.get('system_role', 'DEO')
        captain_cat = data.get('captain_category', 'All')
        modules = data.get('assigned_modules', [])

        if not username or not password:
            return JsonResponse({'status': 'error', 'message': 'Username and Password are required.'}, status=400)

        if User.objects.filter(username__iexact=username).exists():
            return JsonResponse({'status': 'error', 'message': f"Username '{username}' already exists."}, status=400)

        new_user = User.objects.create_user(
            username=username,
            password=password,
            email=email,
            full_name=full_name,
            phone_number=phone_number,
            system_role=role,
            captain_category=captain_cat,
            assigned_modules=modules,
            is_active=True
        )
        log_activity(request.user, 'CREATE', 'User Management', f"Created new login user '{new_user.username}' ({role}).", request)
        return JsonResponse({'status': 'success', 'message': f"User '{new_user.username}' created successfully."})

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)



# ============================================================


# ============================================================


# ============================================================


# ============================================================
# MESS MANAGEMENT SYSTEM VIEWS & APIS (MULTI-MESS & ANALYTICS)
# ============================================================

from datetime import time as dt_time

def _get_or_create_user_employee(user):
    """Helper to ensure logged in user matches their exact Employee record in database."""
    if hasattr(user, 'employee') and user.employee:
        # Protect against legacy admin pointing to a worker
        if (user.is_superuser or user.username.lower() in ['admin', 'administrator']) and user.employee.emp_id != 'ADMIN':
            admin_emp, _ = Employee.objects.get_or_create(
                emp_id='ADMIN',
                defaults={
                    'name': 'System Administrator',
                    'department': 'P&M Department',
                    'designation': 'System Administrator',
                    'contact_info': user.phone_number or '',
                }
            )
            user.employee = admin_emp
            user.save(update_fields=['employee'])
            return admin_emp
        return user.employee

    # 1. Superuser / Admin safety: dedicated System Administrator employee
    if user.is_superuser or user.username.lower() in ['admin', 'administrator']:
        admin_emp, _ = Employee.objects.get_or_create(
            emp_id='ADMIN',
            defaults={
                'name': user.full_name or 'System Administrator',
                'department': 'P&M Department',
                'designation': 'System Administrator',
                'contact_info': user.phone_number or '',
            }
        )
        try:
            user.employee = admin_emp
            user.save(update_fields=['employee'])
        except Exception:
            pass
        return admin_emp

    # 2. Match exact Employee by emp_id, phone, or name (NEVER entered_by!)
    emp_q = Q(emp_id__iexact=user.username)
    if user.phone_number and len(str(user.phone_number).strip()) >= 10:
        emp_q |= Q(contact_info__iexact=str(user.phone_number).strip())
    if user.full_name and len(str(user.full_name).strip()) > 2 and user.full_name.strip().lower() not in ['admin', 'administrator', 'user', 'manager', 'staff']:
        emp_q |= Q(name__iexact=str(user.full_name).strip())

    emp = Employee.objects.filter(emp_q).first()

    clean_post = user.post.name if user.post else ("Staff / Worker" if user.system_role in ['DEO', 'PENDING'] else user.system_role.replace('_', ' ').title())

    if not emp:
        clean_emp_id = user.username
        if '@' in user.username:
            clean_emp_id = f"EMP-{user.id:04d}"

        emp = Employee.objects.create(
            emp_id=clean_emp_id,
            name=user.full_name or user.username.split('@')[0].capitalize(),
            department="Site Operations",
            designation=clean_post,
            contact_info=user.phone_number or '',
        )
    else:
        if emp.designation in ['DEO', 'PENDING', 'Staff', ''] or not emp.designation:
            if user.post:
                emp.designation = user.post.name
                emp.save(update_fields=['designation'])

    try:
        user.employee = emp
        user.save(update_fields=['employee'])
    except Exception:
        pass
    return emp


def _get_active_meal_window_and_timing(employee=None):
    """
    Dynamically evaluates current device/server time against MessMealWindow schedule for Day/Night shifts.
    """
    from portal.models import MessMealWindow
    
    if not MessMealWindow.objects.exists():
        MessMealWindow.objects.create(name="Breakfast", shift="DAY", meal_type="BREAKFAST", start_time=dt_time(6, 0), end_time=dt_time(8, 0), display_order=1)
        MessMealWindow.objects.create(name="Lunch", shift="DAY", meal_type="LUNCH", start_time=dt_time(13, 0), end_time=dt_time(14, 0), display_order=2)
        MessMealWindow.objects.create(name="Dinner", shift="DAY", meal_type="DINNER", start_time=dt_time(20, 0), end_time=dt_time(21, 0), display_order=3)
        MessMealWindow.objects.create(name="Night Refreshment", shift="NIGHT", meal_type="NIGHT_REFRESHMENT", start_time=dt_time(0, 0), end_time=dt_time(2, 0), display_order=1)
        MessMealWindow.objects.create(name="Night Shift Breakfast", shift="NIGHT", meal_type="BREAKFAST", start_time=dt_time(4, 30), end_time=dt_time(6, 30), display_order=2)
        MessMealWindow.objects.create(name="Night Shift Dinner", shift="NIGHT", meal_type="DINNER", start_time=dt_time(21, 30), end_time=dt_time(23, 0), display_order=3)

    user_shift = 'DAY'
    if employee and getattr(employee, 'current_shift', None) == 'Night':
        user_shift = 'NIGHT'

    now_local = timezone.localtime(timezone.now())
    now_time = now_local.time()
    windows = MessMealWindow.objects.filter(is_active=True).filter(Q(shift=user_shift) | Q(shift='ALL')).order_by('display_order', 'start_time')

    active_win = None
    for w in windows:
        if w.start_time <= w.end_time:
            if w.start_time <= now_time <= w.end_time:
                active_win = w
                break
        else:
            if now_time >= w.start_time or now_time <= w.end_time:
                active_win = w
                break

    if not active_win:
        now_hour = now_local.hour
        if 5 <= now_hour < 10:
            active_meal = 'BREAKFAST'
            timing_text = '06:00 AM - 08:00 AM'
        elif 10 <= now_hour < 16:
            active_meal = 'LUNCH'
            timing_text = '01:00 PM - 02:00 PM'
        else:
            active_meal = 'DINNER'
            timing_text = '08:00 PM - 09:00 PM'
    else:
        active_meal = active_win.meal_type
        timing_text = f"{active_win.start_time.strftime('%I:%M %p')} - {active_win.end_time.strftime('%I:%M %p')}"

    status_obj, _ = MessFoodStatus.objects.get_or_create(meal_type=active_meal, defaults={'status': 'CLOSED'})
    return active_meal, timing_text, status_obj


def mess_user_view(request):
    """
    User/Employee Portal View for Mess Management
    """
    if not request.user.is_authenticated:
        return redirect('mess_login_view')

    if not request.user.is_active:
        messages.error(request, "Account is disabled. Please contact Mess Manager.")
        return redirect('mess_login_view')

    allowed_modules = getattr(request.user, 'assigned_modules', []) or []

    today = timezone.now().date()
    emp_profile = _get_or_create_user_employee(request.user)

    active_meal, timing_text, current_status_obj = _get_active_meal_window_and_timing(emp_profile)

    menu_obj = MessMenu.objects.filter(date=today).first()
    today_pass = MessLog.objects.filter(employee=emp_profile, date=today, meal_type=active_meal, status='SUCCESS').first()
    mess_locations = MessLocation.objects.filter(is_active=True)

    has_manager_module = 'mess_manager' in allowed_modules
    has_scanner_module = 'mess_scanner' in allowed_modules
    has_cook_module = 'mess_cook' in allowed_modules
    has_user_module = 'mess_user' in allowed_modules

    if has_manager_module:
        is_manager = True
        is_scanner = has_scanner_module
        is_cook = True
    elif has_scanner_module:
        is_manager = False
        is_scanner = True
        is_cook = has_cook_module
    elif has_cook_module:
        is_manager = False
        is_scanner = False
        is_cook = True
    elif has_user_module:
        is_manager = False
        is_scanner = False
    else:
        is_manager = request.user.is_superuser or request.user.system_role in ['MANAGER', 'PROJECT_MANAGER']
        is_scanner = request.user.system_role in ['TIME_KEEPER', 'DEO']
        is_cook = request.user.is_superuser

    # Determine if user is a pure cafeteria worker/diner
    is_pure_worker = False
    if request.session.get('is_mess_only') or request.user.system_role == 'USER' or (not request.user.is_superuser and request.user.system_role not in ['MANAGER', 'PROJECT_MANAGER', 'TIME_KEEPER']):
        is_pure_worker = True
        is_manager = False
        is_scanner = False
        is_cook = False

    departments = list(Employee.objects.values_list('department', flat=True).distinct())
    departments = [d for d in departments if d]

    authorized_mess_name = "Main Canteen"
    if today_pass and today_pass.mess_location:
        authorized_mess_name = today_pass.mess_location.name
    elif emp_profile and emp_profile.assigned_mess:
        authorized_mess_name = emp_profile.assigned_mess.name
    elif mess_locations.exists():
        authorized_mess_name = mess_locations.first().name

    recent_meal_history = MessLog.objects.filter(
        employee=emp_profile
    ).select_related('mess_location').order_by('-punch_time')[:10]

    return render(request, 'mess_user.html', {
        'active_meal': active_meal,
        'current_timing_text': timing_text,
        'current_status': current_status_obj,
        'menu': menu_obj,
        'today_pass': today_pass,
        'emp_profile': emp_profile,
        'authorized_mess_name': authorized_mess_name,
        'today_date': today,
        'mess_locations': mess_locations,
        'is_manager': is_manager,
        'is_scanner': is_scanner,
        'is_cook': is_cook,
        'is_pure_worker': is_pure_worker,
        'is_manager_or_operator': is_manager or is_scanner,
        'departments': departments,
        'recent_meal_history': recent_meal_history,
    })


@login_required
def mess_kitchen_view(request):
    """
    Mess Kitchen Control Portal for Mess Cooks & Chefs.
    Allows live updating of food readiness status, custom notices, and today's menu.
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    is_authorized = (
        request.user.is_superuser or
        request.user.system_role == 'MANAGER' or
        any(m in allowed_modules for m in ['mess', 'mess_cook', 'mess_manager'])
    )
    if not is_authorized:
        messages.error(request, "Permission Denied: Kitchen Control Portal is reserved for Mess Cook & Management.")
        return redirect('mess_user_view')

    today = timezone.now().date()
    now_hour = timezone.now().hour

    active_meal = 'LUNCH'
    if 5 <= now_hour < 10:
        active_meal = 'BREAKFAST'
    elif 10 <= now_hour < 16:
        active_meal = 'LUNCH'
    else:
        active_meal = 'DINNER'

    bf_status, _ = MessFoodStatus.objects.get_or_create(meal_type='BREAKFAST', defaults={'status': 'CLOSED'})
    lunch_status, _ = MessFoodStatus.objects.get_or_create(meal_type='LUNCH', defaults={'status': 'CLOSED'})
    dinner_status, _ = MessFoodStatus.objects.get_or_create(meal_type='DINNER', defaults={'status': 'CLOSED'})

    menu_obj, _ = MessMenu.objects.get_or_create(date=today)

    bf_count = MessLog.objects.filter(date=today, meal_type='BREAKFAST', status='SUCCESS').count()
    lunch_count = MessLog.objects.filter(date=today, meal_type='LUNCH', status='SUCCESS').count()
    dinner_count = MessLog.objects.filter(date=today, meal_type='DINNER', status='SUCCESS').count()

    is_manager = request.user.is_superuser or request.user.system_role == 'MANAGER' or ('mess_manager' in allowed_modules)

    return render(request, 'mess_kitchen.html', {
        'active_meal': active_meal,
        'bf_status': bf_status,
        'lunch_status': lunch_status,
        'dinner_status': dinner_status,
        'menu': menu_obj,
        'bf_count': bf_count,
        'lunch_count': lunch_count,
        'dinner_count': dinner_count,
        'today_date': today,
        'is_manager': is_manager,
    })


@csrf_exempt
@login_required
def api_update_food_status(request):
    """
    API endpoint for cooks to update live food readiness status & announcement.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        meal_type = data.get('meal_type', 'LUNCH').upper()
        status = data.get('status', 'READY').upper()
        announcement = data.get('announcement', None)

        food_status, _ = MessFoodStatus.objects.get_or_create(meal_type=meal_type)
        food_status.status = status
        if announcement is not None:
            food_status.announcement = announcement.strip()
        food_status.updated_by = request.user
        food_status.save()

        return JsonResponse({
            'status': 'SUCCESS',
            'msg': f'Status for {meal_type} updated to "{food_status.get_status_display()}" successfully!',
            'status_display': food_status.get_status_display(),
            'updated_at': food_status.updated_at.strftime('%H:%M:%S'),
            'updated_by': request.user.get_full_name() or request.user.username
        })
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_update_mess_menu(request):
    """
    API endpoint for cooks to update today's breakfast, lunch, and dinner menu.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        today = timezone.now().date()
        menu_obj, _ = MessMenu.objects.get_or_create(date=today)

        if 'breakfast_menu' in data:
            menu_obj.breakfast_menu = str(data['breakfast_menu']).strip()
        if 'lunch_menu' in data:
            menu_obj.lunch_menu = str(data['lunch_menu']).strip()
        if 'dinner_menu' in data:
            menu_obj.dinner_menu = str(data['dinner_menu']).strip()

        menu_obj.save()
        return JsonResponse({'status': 'SUCCESS', 'msg': "Today's Mess Menu updated successfully!"})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


def _claim_meal_for_user_if_requested(request, user, claim_mess_id):
    """Helper to auto-claim meal pass when user logs in or registers via Mess Wall QR scan."""
    if not claim_mess_id:
        return
    mess_loc = MessLocation.objects.filter(id=claim_mess_id, is_active=True).first()
    if not mess_loc:
        return
    emp = _get_or_create_user_employee(user)
    if not emp:
        return

    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    active_meal, _, _ = _get_active_meal_window_and_timing(emp)

    existing = MessLog.objects.filter(
        employee=emp,
        date=today,
        meal_type=active_meal,
        status='SUCCESS'
    ).first()

    if existing:
        orig_mess_name = existing.mess_location.name if existing.mess_location else mess_loc.name
        is_diff_mess = bool(existing.mess_location and existing.mess_location.id != mess_loc.id)
        if is_diff_mess:
            MessLog.objects.create(
                employee=emp,
                mess_location=mess_loc,
                date=today,
                meal_type=active_meal,
                scanned_by=user,
                status='DUPLICATE',
                remarks=f"MULTI-MESS duplicate claim: Already eaten at {orig_mess_name}"
            )
            messages.error(request, f"⛔ MULTI-MESS ALERT: {emp.name}, you have already claimed {active_meal} at '{orig_mess_name}'! You cannot take meals from multiple mess locations.")
        else:
            messages.info(request, f"ℹ️ {emp.name}, your {active_meal} pass is already active ({existing.verification_token}).")
    else:
        import uuid
        token = f"PASS-{uuid.uuid4().hex[:8].upper()}"
        MessLog.objects.create(
            employee=emp,
            mess_location=mess_loc,
            date=today,
            meal_type=active_meal,
            scanned_by=user,
            status='SUCCESS',
            entry_mode='WALL_QR_CLAIM',
            verification_token=token,
            remarks=f"Wall QR Pass claimed at {mess_loc.name}"
        )
        messages.success(request, f"✅ Meal Pass Confirmed! {emp.name}, your {active_meal} pass has been successfully activated at {mess_loc.name}.")


@csrf_exempt
@never_cache
def mess_login_view(request):
    """
    Dedicated, mobile-optimized Login Portal for Mess Workers / General Employees.
    Supports:
    1. Direct Login via Employee ID, Phone Number, or Username + PIN/Password.
    2. First-Time PIN Activation for existing 4,055 DB employees.
    3. Self-Registration for new workers not yet in DB.
    Auto-claims pass if claim_mess parameter is provided.
    """
    claim_mess_id = request.GET.get('claim_mess') or request.POST.get('claim_mess')
    claim_mess = None
    if claim_mess_id:
        claim_mess = MessLocation.objects.filter(id=claim_mess_id, is_active=True).first()

    if request.user.is_authenticated:
        if claim_mess:
            _claim_meal_for_user_if_requested(request, request.user, claim_mess.id)
        return redirect('mess_user_view')

    error_msg = None
    if request.GET.get('csrf_retry'):
        error_msg = "Your browser security token refreshed. Please enter your credentials to log in."
    success_msg = None
    active_tab = 'login'

    if request.method == 'POST':
        action = request.POST.get('action', 'login')

        if action == 'login':
            active_tab = 'login'
            login_identifier = str(request.POST.get('login_identifier', '')).strip()
            raw_password = str(request.POST.get('password', ''))
            password = raw_password.strip()

            emp_obj, user_obj = _resolve_worker_and_user(login_identifier)

            if not emp_obj and not user_obj:
                error_msg = f"Worker ID / Phone '{login_identifier}' was not found in the database! If you are a new worker, please register via the 'New Worker' tab."
            elif emp_obj and not user_obj:
                # Employee exists in roster (one of 4,055) but has not set a PIN yet!
                error_msg = f"Worker '{emp_obj.name}' ({emp_obj.emp_id}) is in the database, but a PIN has not been set yet! Please use the 'Set PIN' tab to create your 4-digit PIN."
                active_tab = 'activate'
            elif user_obj:
                # Ensure user is linked to employee if not already
                if emp_obj and not user_obj.employee:
                    user_obj.employee = emp_obj
                    user_obj.save(update_fields=['employee'])

                # Check if PIN has been set
                if not user_obj.has_usable_password():
                    worker_display = emp_obj.name if emp_obj else (user_obj.full_name or user_obj.username)
                    error_msg = f"Worker '{worker_display}' has not set a 4-digit PIN yet! Please use the 'Set PIN' tab to create your PIN."
                    active_tab = 'activate'
                else:
                    user = authenticate(request, username=user_obj.username, password=password)
                    if user is None and raw_password != password:
                        user = authenticate(request, username=user_obj.username, password=raw_password)

                    # Direct fallback for check_password
                    if user is None:
                        if user_obj.check_password(password) or user_obj.check_password(raw_password):
                            user = user_obj
                            user.backend = 'django.contrib.auth.backends.ModelBackend'

                    if user is not None:
                        if not user.is_active:
                            error_msg = "Your account is disabled. Please contact the Mess Incharge or Administrator."
                        else:
                            login(request, user)
                            request.session.set_expiry(60 * 60 * 24 * 365) # 1 Year persistent login
                            request.session['is_mess_only'] = True
                            request.session.modified = True
                            log_activity(user, 'LOGIN', 'Mess Auth', f"{user.full_name or user.username} logged into Mess Worker Portal.", request)
                            if claim_mess:
                                _claim_meal_for_user_if_requested(request, user, claim_mess.id)
                            return redirect('mess_user_view')
                    else:
                        error_msg = "Invalid PIN or Password! If you are logging in for the first time or forgot your PIN, please use the 'Set PIN' tab."

        elif action == 'activate_pin':
            active_tab = 'activate'
            emp_id_input = str(request.POST.get('emp_id_or_phone', '')).strip()
            verify_phone = str(request.POST.get('verify_phone', '')).strip()
            new_pin = str(request.POST.get('new_pin', '')).strip()
            confirm_pin = str(request.POST.get('confirm_pin', '')).strip()

            if not emp_id_input:
                error_msg = "Please enter your Employee ID or Mobile Number."
            elif not new_pin or len(new_pin) < 4:
                error_msg = "The PIN must be at least 4 digits."
            elif new_pin != confirm_pin:
                error_msg = "The PINs do not match! Please verify and try again."
            else:
                # Search employee & user using robust resolver
                emp_obj, user_obj = _resolve_worker_and_user(emp_id_input)
                if not emp_obj and verify_phone:
                    emp_obj, user_obj = _resolve_worker_and_user(verify_phone)

                if not emp_obj and not user_obj:
                    error_msg = f"Employee ID / Phone '{emp_id_input}' was not found in the database. Please register via the 'New Worker' tab."
                else:
                    clean_username = (emp_obj.emp_id.strip() if emp_obj and emp_obj.emp_id else (user_obj.username if user_obj else emp_id_input)).strip()
                    if not user_obj:
                        try:
                            user_obj = User(
                                username=clean_username,
                                full_name=emp_obj.name if emp_obj else clean_username,
                                phone_number=emp_obj.contact_info if (emp_obj and emp_obj.contact_info) else verify_phone,
                                system_role='USER',
                                assigned_modules=['mess_user'],
                                is_active=True
                            )
                            user_obj.set_password(new_pin)
                            user_obj.employee = emp_obj
                            user_obj.save()
                        except IntegrityError:
                            user_obj = User.objects.filter(
                                Q(username__iexact=clean_username) |
                                Q(phone_number__iexact=emp_id_input)
                            ).first()

                    if user_obj:
                        if not user_obj.system_role or user_obj.system_role in ['USER', 'PENDING']:
                            user_obj.system_role = 'USER'
                        user_obj.assigned_modules = list(set((user_obj.assigned_modules or []) + ['mess_user']))
                        user_obj.set_password(new_pin)
                        user_obj.is_active = True
                        if emp_obj:
                            user_obj.employee = emp_obj
                            if not user_obj.full_name:
                                user_obj.full_name = emp_obj.name
                            if not user_obj.phone_number and emp_obj.contact_info:
                                user_obj.phone_number = emp_obj.contact_info
                        user_obj.save()

                        if emp_obj:
                            emp_obj.entered_by = user_obj
                            emp_obj.save(update_fields=['entered_by'])

                        user_to_login = authenticate(request, username=user_obj.username, password=new_pin)
                        if not user_to_login and user_obj.check_password(new_pin):
                            user_to_login = user_obj
                            user_to_login.backend = 'django.contrib.auth.backends.ModelBackend'

                        if user_to_login:
                            login(request, user_to_login)
                            request.session.set_expiry(60 * 60 * 24 * 365) # 1 Year
                            request.session['is_mess_only'] = True
                            request.session.modified = True
                            worker_name = emp_obj.name if emp_obj else (user_obj.full_name or user_obj.username)
                            log_activity(user_to_login, 'ACTIVATE_PIN', 'Mess Auth', f"{worker_name} set their mess PIN.", request)
                            if claim_mess:
                                _claim_meal_for_user_if_requested(request, user_to_login, claim_mess.id)
                            else:
                                messages.success(request, f"Welcome {worker_name}! Your 4-digit PIN has been set successfully.")
                            return redirect('mess_user_view')
                        else:
                            success_msg = "PIN has been set successfully! You can now log in."
                            active_tab = 'login'
                    else:
                        error_msg = "Failed to activate PIN. Please try logging in."

        elif action == 'self_register':
            active_tab = 'register'
            full_name = str(request.POST.get('full_name', '')).strip()
            phone_number = str(request.POST.get('phone_number', '')).strip()
            emp_id_input = str(request.POST.get('emp_id', '')).strip()
            permit_number = str(request.POST.get('permit_number', '')).strip()
            pin = str(request.POST.get('pin', '')).strip()

            if not full_name or not phone_number or not emp_id_input:
                error_msg = "Please enter Full Name, Employee ID, and Mobile Number."
            elif not pin or len(pin) < 4:
                error_msg = "The PIN must be at least 4 digits."
            else:
                clean_emp_id = emp_id_input.strip()
                default_mess = MessLocation.objects.filter(is_active=True).first()

                # 1. Find or create Employee safely using robust resolver
                emp_obj, user_obj = _resolve_worker_and_user(clean_emp_id)
                if not emp_obj and phone_number:
                    emp_obj, user_obj = _resolve_worker_and_user(phone_number)

                if emp_obj:
                    changed_fields = []
                    if permit_number and not emp_obj.work_permit_no:
                        emp_obj.work_permit_no = permit_number
                        changed_fields.append('work_permit_no')
                    if not emp_obj.assigned_mess and default_mess:
                        emp_obj.assigned_mess = default_mess
                        changed_fields.append('assigned_mess')
                    if changed_fields:
                        emp_obj.save(update_fields=changed_fields)
                else:
                    emp_obj = Employee.objects.create(
                        emp_id=clean_emp_id,
                        name=full_name,
                        department="Site Operations",
                        designation="Worker / Diner",
                        contact_info=phone_number,
                        work_permit_no=permit_number,
                        assigned_mess=default_mess
                    )

                # 2. Find or create / update User safely without IntegrityError
                if not user_obj:
                    user_obj = User.objects.filter(
                        Q(username__iexact=clean_emp_id) |
                        Q(employee=emp_obj) |
                        Q(phone_number__iexact=phone_number)
                    ).first()

                if not user_obj:
                    try:
                        user_obj = User(
                            username=clean_emp_id,
                            full_name=full_name or emp_obj.name,
                            phone_number=phone_number or emp_obj.contact_info,
                            system_role='USER',
                            assigned_modules=['mess_user'],
                            is_active=True
                        )
                        user_obj.set_password(pin)
                        user_obj.save()
                    except IntegrityError:
                        user_obj = User.objects.filter(
                            Q(username__iexact=clean_emp_id) |
                            Q(phone_number__iexact=phone_number)
                        ).first()

                if user_obj:
                    if not user_obj.system_role or user_obj.system_role in ['USER', 'PENDING']:
                        user_obj.system_role = 'USER'
                    user_obj.assigned_modules = list(set((user_obj.assigned_modules or []) + ['mess_user']))
                    user_obj.set_password(pin)
                    user_obj.employee = emp_obj
                    user_obj.is_active = True
                    if full_name:
                        user_obj.full_name = full_name
                    if phone_number:
                        user_obj.phone_number = phone_number
                    user_obj.save()

                    emp_obj.entered_by = user_obj
                    emp_obj.save(update_fields=['entered_by'])

                    user_to_login = authenticate(request, username=user_obj.username, password=pin)
                    if not user_to_login and user_obj.check_password(pin):
                        user_to_login = user_obj
                        user_to_login.backend = 'django.contrib.auth.backends.ModelBackend'

                    if user_to_login:
                        login(request, user_to_login)
                        request.session.set_expiry(60 * 60 * 24 * 365) # 1 Year
                        request.session['is_mess_only'] = True
                        request.session.modified = True
                        log_activity(user_to_login, 'REGISTER', 'Mess Auth', f"{emp_obj.name} registered / updated worker mess account ({clean_emp_id}).", request)
                        if claim_mess:
                            _claim_meal_for_user_if_requested(request, user_to_login, claim_mess.id)
                        else:
                            messages.success(request, f"Welcome {emp_obj.name}! Your account is ready.")
                        return redirect('mess_user_view')
                    else:
                        success_msg = "Account registered successfully! You can now log in."
                        active_tab = 'login'
                else:
                    error_msg = "Error processing account. Please try again."

    # Pre-fetch today's public menu and active mess locations for workers
    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    today_menu = MessMenu.objects.filter(date=today).first()
    active_meal, current_timing_text, current_status = _get_active_meal_window_and_timing(None)
    mess_locations = MessLocation.objects.filter(is_active=True).order_by('name')

    return render(request, 'mess_login.html', {
        'error_msg': error_msg,
        'success_msg': success_msg,
        'active_tab': active_tab,
        'today_menu': today_menu,
        'active_meal': active_meal,
        'current_timing_text': current_timing_text,
        'current_status': current_status,
        'today_date': today,
        'mess_locations': mess_locations,
        'claim_mess': claim_mess,
    })


def mess_root_view(request):
    """
    Root redirect for /mess/ endpoint according to user role:
    - If unauthenticated -> /mess/login/
    - Manager -> /mess/monitor/
    - Scanner / Timekeeper -> /mess/scan/
    - Mess Cook / Chef -> /mess/kitchen/
    - Normal Employee / User -> /mess/user/
    """
    if not request.user.is_authenticated:
        return redirect('mess_login_view')

    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    has_manager_module = 'mess_manager' in allowed_modules
    has_scanner_module = 'mess_scanner' in allowed_modules
    has_cook_module = 'mess_cook' in allowed_modules
    has_user_module = 'mess_user' in allowed_modules

    if has_manager_module:
        return redirect('mess_monitor_view')
    elif has_scanner_module:
        return redirect('mess_scan_view')
    elif has_cook_module:
        return redirect('mess_kitchen_view')
    elif has_user_module:
        return redirect('mess_user_view')
    else:
        if request.user.is_superuser or request.user.system_role in ['MANAGER', 'PROJECT_MANAGER']:
            return redirect('mess_monitor_view')
        elif request.user.system_role in ['TIME_KEEPER', 'DEO']:
            return redirect('mess_scan_view')
        else:
            return redirect('mess_user_view')


@csrf_exempt
@login_required
def api_generate_self_pass(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid method'}, status=405)

    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    emp = _get_or_create_user_employee(request.user)

    active_meal, timing_text, _ = _get_active_meal_window_and_timing(emp)

    mess_id = request.POST.get('mess_id') or (request.GET.get('mess_id') if request.method == 'GET' else None)
    mess_loc = None
    if mess_id and str(mess_id).isdigit():
        mess_loc = MessLocation.objects.filter(id=int(mess_id)).first()
    if not mess_loc:
        mess_loc = emp.assigned_mess or MessLocation.objects.filter(is_active=True).first()

    existing = MessLog.objects.filter(employee=emp, date=today, meal_type=active_meal, status='SUCCESS').first()
    if existing:
        original_time = timezone.localtime(existing.punch_time).strftime('%I:%M:%S %p')
        orig_mess_name = existing.mess_location.name if existing.mess_location else (mess_loc.name if mess_loc else 'Main Canteen')
        return JsonResponse({
            'status': 'EXISTING',
            'token': existing.verification_token,
            'time': original_time,
            'meal': active_meal,
            'mess_name': orig_mess_name,
            'msg': f"Pass already issued for {active_meal} at {original_time} (Valid ONLY at '{orig_mess_name}')."
        })

    import uuid
    token = f"PASS-{uuid.uuid4().hex[:8].upper()}"

    new_log = MessLog.objects.create(
        employee=emp,
        mess_location=mess_loc,
        date=today,
        meal_type=active_meal,
        scanned_by=request.user,
        status='SUCCESS',
        entry_mode='WALL_QR_SELF_PASS',
        verification_token=token,
        remarks=f"Self-Pass generated for {mess_loc.name if mess_loc else 'Main Canteen'}"
    )

    mess_name_str = mess_loc.name if mess_loc else 'Main Canteen'
    return JsonResponse({
        'status': 'SUCCESS',
        'token': token,
        'time': timezone.localtime(new_log.punch_time).strftime('%I:%M:%S %p'),
        'meal': active_meal,
        'mess_name': mess_name_str,
        'msg': f"Digital Food Pass generated successfully for {mess_name_str}!"
    })


@csrf_exempt
@login_required
def api_submit_mess_feedback(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        tag = data.get('feedback_tag', 'GOOD')
        meal = data.get('meal_type', 'LUNCH')
        comments = data.get('comments', '')
        today = timezone.now().date()

        emp = _get_or_create_user_employee(request.user)

        MessFeedback.objects.create(
            employee=emp,
            date=today,
            meal_type=meal,
            feedback_tag=tag,
            comments=comments
        )
        return JsonResponse({'status': 'SUCCESS', 'msg': 'Feedback submitted successfully!'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def mess_monitor_view(request):
    """
    Master Executive Monitor Dashboard: Multi-Mess Comparative Analytics & Filtered Fed Workers List
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    has_manager_module = 'mess_manager' in allowed_modules
    has_scanner_module = 'mess_scanner' in allowed_modules
    has_user_module = 'mess_user' in allowed_modules

    if has_manager_module:
        is_authorized = True
    elif has_user_module or has_scanner_module:
        is_authorized = False
    else:
        is_authorized = request.user.is_superuser or request.user.system_role in ['MANAGER', 'PROJECT_MANAGER']

    if not is_authorized:
        messages.error(request, "Access Restricted: Executive Mess Monitor is reserved for Management.")
        if has_scanner_module or request.user.system_role in ['TIME_KEEPER', 'DEO']:
            return redirect('mess_scan_view')
        return redirect('mess_user_view')

    today_str = timezone.now().date().strftime('%Y-%m-%d')
    from_date_str = request.GET.get('from_date', today_str)
    to_date_str = request.GET.get('to_date', today_str)
    selected_mess = request.GET.get('mess_id', 'ALL')
    selected_meal = request.GET.get('meal_type', 'ALL')
    selected_dept = request.GET.get('department', 'ALL')
    search_query = request.GET.get('search', '').strip()

    try:
        from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
    except ValueError:
        from_date = timezone.now().date()

    try:
        to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
    except ValueError:
        to_date = timezone.now().date()

    today = timezone.now().date()
    now_hour = timezone.now().hour

    active_meal = 'LUNCH'
    if 5 <= now_hour < 10: active_meal = 'BREAKFAST'
    elif 10 <= now_hour < 16: active_meal = 'LUNCH'
    else: active_meal = 'DINNER'

    today_logs = MessLog.objects.filter(date=today)

    # Master KPIs Computation
    total_eligible = Employee.objects.filter(status='Active').count() or 1
    total_fed = today_logs.filter(status='SUCCESS').values('employee').distinct().count()
    total_remaining = max(0, total_eligible - total_fed)
    total_blocked = today_logs.filter(status__in=['DUPLICATE', 'INVALID']).count()
    total_absent = EmployeeAttendance.objects.filter(date=today, status='Absent').count()
    fed_pct = round((total_fed / total_eligible) * 100, 1)

    kpi_master = {
        'total_eligible': total_eligible,
        'total_fed': total_fed,
        'total_remaining': total_remaining,
        'total_blocked': total_blocked,
        'total_absent': total_absent,
        'fed_percentage': fed_pct
    }

    # Mess Location Breakdown
    locations = MessLocation.objects.filter(is_active=True)
    mess_breakdown = []
    for loc in locations:
        fed_loc = today_logs.filter(mess_location=loc, status='SUCCESS').count()
        errors_loc = today_logs.filter(mess_location=loc, status__in=['DUPLICATE', 'INVALID']).count()
        pct = round((fed_loc / (loc.capacity or 100)) * 100, 1) if loc.capacity else 0
        status_loc = MessFoodStatus.objects.filter(meal_type=active_meal).first()
        mess_breakdown.append({
            'id': loc.id,
            'name': loc.name,
            'code': loc.code,
            'capacity': loc.capacity,
            'contractor_agency': loc.contractor_agency or '',
            'fed': fed_loc,
            'errors': errors_loc,
            'progress_pct': min(100, pct),
            'status': status_loc.get_status_display() if status_loc else 'Closed'
        })

    # FED WORKERS FILTERED LOGS (ONLY SHOWING WORKERS WHO HAVE EATEN)
    fed_logs_qs = MessLog.objects.filter(
        date__range=[from_date, to_date],
        status='SUCCESS'
    ).select_related('employee', 'mess_location', 'scanned_by')

    if selected_mess != 'ALL' and selected_mess:
        fed_logs_qs = fed_logs_qs.filter(mess_location_id=selected_mess)

    if selected_meal != 'ALL' and selected_meal:
        fed_logs_qs = fed_logs_qs.filter(meal_type=selected_meal)

    if selected_dept != 'ALL' and selected_dept:
        fed_logs_qs = fed_logs_qs.filter(employee__department=selected_dept)

    if search_query:
        fed_logs_qs = fed_logs_qs.filter(
            Q(employee__name__icontains=search_query) |
            Q(employee__emp_id__icontains=search_query)
        )

    fed_logs = fed_logs_qs.order_by('-punch_time')[:100]
    filtered_fed_count = fed_logs_qs.count()

    # REMAINING / UNFED WORKERS TODAY (Active workers who have NOT eaten today yet)
    fed_emp_ids = today_logs.filter(status='SUCCESS').values_list('employee_id', flat=True)
    unfed_qs = Employee.objects.filter(status='Active').exclude(id__in=fed_emp_ids)
    
    if selected_mess != 'ALL' and selected_mess:
        unfed_qs = unfed_qs.filter(assigned_mess_id=selected_mess)
    if selected_dept != 'ALL' and selected_dept:
        unfed_qs = unfed_qs.filter(department=selected_dept)
    if search_query:
        unfed_qs = unfed_qs.filter(Q(name__icontains=search_query) | Q(emp_id__icontains=search_query))

    unfed_count = unfed_qs.count()
    unfed_workers = unfed_qs.select_related('assigned_mess').order_by('name')[:50]
    all_active_employees = Employee.objects.filter(status='Active').select_related('assigned_mess').order_by('name')[:100]

    menu_obj = MessMenu.objects.filter(date=today).first()
    status_obj, _ = MessFoodStatus.objects.get_or_create(meal_type=active_meal, defaults={'status': 'CLOSED'})

    feedbacks = MessFeedback.objects.filter(date=today).select_related('employee').order_by('-created_at')[:50]
    departments = list(Employee.objects.values_list('department', flat=True).distinct())
    departments = [d for d in departments if d]

    from portal.models import MessMealWindow
    meal_windows = MessMealWindow.objects.all().order_by('shift', 'display_order', 'start_time')

    # Calculate Inactive Employees (No Mess Punches for 15+ Days)
    fifteen_days_ago = today - timedelta(days=15)
    from django.db.models import Max
    emp_latest_mess = MessLog.objects.filter(status='SUCCESS').values('employee_id').annotate(latest_date=Max('date'))
    latest_dict = {x['employee_id']: x['latest_date'] for x in emp_latest_mess}

    inactive_workers = []
    for emp in Employee.objects.filter(status='Active').select_related('assigned_mess'):
        last_date = latest_dict.get(emp.id)
        if last_date:
            if last_date <= fifteen_days_ago:
                days_absent = (today - last_date).days
                inactive_workers.append({
                    'id': emp.id,
                    'emp_id': emp.emp_id,
                    'name': emp.name,
                    'department': str(emp.department or 'General'),
                    'designation': str(emp.designation or 'Worker'),
                    'assigned_mess': emp.assigned_mess.name if emp.assigned_mess else 'Main Mess',
                    'last_date': last_date.strftime('%d-%b-%Y'),
                    'days_absent': days_absent,
                    'status_note': f"{days_absent} days without mess meals"
                })
        else:
            # Never scanned in mess
            join_d = getattr(emp, 'date_of_joining', None) or getattr(emp, 'created_at', None)
            join_date = join_d.date() if hasattr(join_d, 'date') else (join_d if join_d else None)
            if join_date and join_date <= fifteen_days_ago:
                days_absent = (today - join_date).days
                inactive_workers.append({
                    'id': emp.id,
                    'emp_id': emp.emp_id,
                    'name': emp.name,
                    'department': str(emp.department or 'General'),
                    'designation': str(emp.designation or 'Worker'),
                    'assigned_mess': emp.assigned_mess.name if emp.assigned_mess else 'Main Mess',
                    'last_date': 'Never Scanned',
                    'days_absent': days_absent,
                    'status_note': f"Never scanned ({days_absent}+ days on site)"
                })

    inactive_workers.sort(key=lambda x: x['days_absent'], reverse=True)
    inactive_count = len(inactive_workers)

    timekeepers = User.objects.filter(Q(system_role__in=['TIME_KEEPER', 'DEO', 'SCANNER']) | Q(is_superuser=True)).order_by('username')

    return render(request, 'mess_monitor.html', {
        'active_meal': active_meal,
        'menu': menu_obj,
        'current_status': status_obj,
        'kpi_master': kpi_master,
        'mess_breakdown': mess_breakdown,
        'mess_locations': locations,
        'timekeepers': timekeepers,
        'fed_logs': fed_logs,
        'filtered_fed_count': filtered_fed_count,
        'unfed_workers': unfed_workers,
        'unfed_count': unfed_count,
        'inactive_workers': inactive_workers,
        'inactive_count': inactive_count,
        'all_active_employees': all_active_employees,
        'from_date': from_date_str,
        'to_date': to_date_str,
        'selected_mess': selected_mess,
        'selected_meal': selected_meal,
        'selected_dept': selected_dept,
        'search_query': search_query,
        'feedbacks': feedbacks,
        'today_date': today,
        'departments': departments,
        'meal_windows': meal_windows,
        'camp_inside_count': Employee.objects.filter(status='Active', camp_status='INSIDE').count(),
    })


@csrf_exempt
@login_required
def api_mess_mark_worker_resigned(request):
    """
    API endpoint for Mess Incharge / Manager to mark an inactive worker as Resigned or Inactive
    and free their camp room / mess allotments when they haven't eaten for 15+ days.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
        emp_id = data.get('emp_id')
        action_type = str(data.get('action', 'RESIGNED')).upper()
        remarks = str(data.get('remarks', 'Marked resigned due to 15+ days absence in mess')).strip()

        emp = None
        if str(emp_id).isdigit():
            emp = Employee.objects.filter(id=int(emp_id)).first()
        if not emp:
            emp = Employee.objects.filter(emp_id=emp_id).first()

        if not emp:
            return JsonResponse({'status': 'ERROR', 'msg': 'Employee record not found.'}, status=404)

        emp.status = 'Resigned' if action_type == 'RESIGNED' else 'Inactive'
        emp.camp_status = 'OUTSIDE'
        emp.save(update_fields=['status', 'camp_status'])

        # Free room allocation if any
        try:
            from portal.models import CampAssetAllotment
            allot = CampAssetAllotment.objects.filter(employee=emp).first()
            if allot and allot.room:
                allot.room = None
                allot.bed_number = ''
                allot.save()
        except Exception:
            pass

        return JsonResponse({
            'status': 'SUCCESS',
            'msg': f"Worker {emp.name} ({emp.emp_id}) marked as '{emp.status}'. Mess & Camp records updated."
        })
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_save_meal_window(request):
    """
    API endpoint for Managers to create or update Meal Window Schedules for Day/Night shifts.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        window_id = data.get('id')
        name = str(data.get('name', '')).strip()
        shift = str(data.get('shift', 'DAY')).upper()
        meal_type = str(data.get('meal_type', 'LUNCH')).upper()
        start_str = str(data.get('start_time', '06:00')).strip()
        end_str = str(data.get('end_time', '08:00')).strip()

        if not name:
            return JsonResponse({'status': 'ERROR', 'msg': 'Meal window schedule name is required.'}, status=400)

        start_t = datetime.strptime(start_str, '%H:%M').time() if len(start_str) <= 5 else datetime.strptime(start_str, '%H:%M:%S').time()
        end_t = datetime.strptime(end_str, '%H:%M').time() if len(end_str) <= 5 else datetime.strptime(end_str, '%H:%M:%S').time()

        from portal.models import MessMealWindow
        if window_id:
            win = MessMealWindow.objects.get(id=window_id)
            win.name = name
            win.shift = shift
            win.meal_type = meal_type
            win.start_time = start_t
            win.end_time = end_t
            win.save()
            msg = f"Meal Window Schedule '{name}' updated successfully!"
        else:
            win = MessMealWindow.objects.create(
                name=name,
                shift=shift,
                meal_type=meal_type,
                start_time=start_t,
                end_time=end_t
            )
            msg = f"New Meal Window Schedule '{name}' added successfully!"

        return JsonResponse({'status': 'SUCCESS', 'msg': msg})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_delete_meal_window(request):
    """
    API endpoint for Managers to delete a Meal Window Schedule.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        window_id = data.get('id')
        from portal.models import MessMealWindow
        MessMealWindow.objects.filter(id=window_id).delete()
        return JsonResponse({'status': 'SUCCESS', 'msg': 'Meal Window Schedule deleted successfully!'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_update_food_status(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        status_val = data.get('status', 'CLOSED')
        meal = data.get('meal_type', 'LUNCH')

        status_obj, _ = MessFoodStatus.objects.get_or_create(meal_type=meal)
        status_obj.status = status_val
        status_obj.updated_by = request.user
        status_obj.save()

        return JsonResponse({'status': 'SUCCESS', 'msg': f'Status updated to {status_val}'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def api_update_mess_menu(request):
    if request.method == 'POST':
        today = timezone.now().date()
        menu_obj, _ = MessMenu.objects.get_or_create(date=today)
        menu_obj.breakfast_menu = request.POST.get('breakfast_menu', '')
        menu_obj.lunch_menu = request.POST.get('lunch_menu', '')
        menu_obj.dinner_menu = request.POST.get('dinner_menu', '')
        menu_obj.save()
        messages.success(request, "Mess Menu updated successfully!")
    return redirect('mess_monitor_view')


@login_required
def mess_scan_view(request):
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    has_manager_module = 'mess_manager' in allowed_modules
    has_scanner_module = 'mess_scanner' in allowed_modules
    has_user_module = 'mess_user' in allowed_modules

    if has_scanner_module or has_manager_module:
        is_authorized = True
    elif has_user_module:
        is_authorized = False
    else:
        is_authorized = request.user.is_superuser or request.user.system_role in ['MANAGER', 'TIME_KEEPER', 'DEO']

    if not is_authorized:
        messages.error(request, "Permission Denied: PDA Scanner is reserved for Scanner Operators and Managers.")
        return redirect('mess_user_view')

    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    now_hour = now_local.hour

    active_meal = 'LUNCH'
    if 5 <= now_hour < 10: active_meal = 'BREAKFAST'
    elif 10 <= now_hour < 16: active_meal = 'LUNCH'
    else: active_meal = 'DINNER'

    recent_logs = MessLog.objects.filter(date=today).select_related('employee', 'mess_location').order_by('-punch_time')[:25]
    locations = MessLocation.objects.filter(is_active=True)

    kpi = {
        'breakfast': MessLog.objects.filter(date=today, meal_type='BREAKFAST', status='SUCCESS').count(),
        'lunch': MessLog.objects.filter(date=today, meal_type='LUNCH', status='SUCCESS').count(),
        'snacks': MessLog.objects.filter(date=today, meal_type='SNACKS', status='SUCCESS').count(),
        'dinner': MessLog.objects.filter(date=today, meal_type='DINNER', status='SUCCESS').count(),
        'total': MessLog.objects.filter(date=today, status='SUCCESS').count(),
    }

    return render(request, 'mess_scan.html', {
        'active_meal': active_meal,
        'recent_logs': recent_logs,
        'kpi': kpi,
        'mess_locations': locations,
        'current_time_display': now_local.strftime('%I:%M:%S %p'),
        'current_date_display': now_local.strftime('%d %b %Y'),
    })


@csrf_exempt
@login_required
def api_mess_punch(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        code = str(data.get('code', '')).strip()
        meal_type = str(data.get('meal_type', 'LUNCH')).strip()
        mess_id = data.get('mess_id')
        now_local = timezone.localtime(timezone.now())
        today = now_local.date()

        mess_loc = MessLocation.objects.filter(id=mess_id).first() if mess_id else None

        if not code:
            return JsonResponse({'status': 'ERROR', 'msg': 'No barcode or QR code scanned.'}, status=400)

        token_match = MessLog.objects.filter(verification_token__iexact=code, date=today).first()
        if token_match:
            employee = token_match.employee
        else:
            employee = Employee.objects.filter(
                Q(emp_id__iexact=code) | Q(name__iexact=code) | Q(contact_info__iexact=code)
            ).first()

        if not employee:
            return JsonResponse({'status': 'ERROR', 'msg': f"Invalid Card/Pass: '{code}' not found in employee roster."}, status=404)

        # Check if pass token belongs specifically to another mess
        if token_match and token_match.mess_location and mess_loc and token_match.mess_location.id != mess_loc.id:
            return JsonResponse({
                'status': 'ERROR',
                'msg': f"⛔ WRONG MESS LOCATION! This pass is valid ONLY at '{token_match.mess_location.name}'. You cannot scan or collect food at '{mess_loc.name}'."
            }, status=400)

        # Check if already punched today for this meal window
        original_log = MessLog.objects.filter(
            employee=employee,
            date=today,
            meal_type=meal_type,
            status='SUCCESS'
        ).order_by('punch_time').first()

        # If token_match is the exact same valid token and already marked, treat as re-scan
        if original_log:
            orig_time_str = timezone.localtime(original_log.punch_time).strftime('%I:%M:%S %p')
            orig_date_str = original_log.date.strftime('%d %b %Y')
            orig_mess_name = original_log.mess_location.name if original_log.mess_location else 'Main Mess'
            curr_mess_name = mess_loc.name if mess_loc else 'Current Mess'
            is_different_mess = bool(original_log.mess_location and mess_loc and original_log.mess_location.id != mess_loc.id)

            remarks_text = f"MULTI-MESS duplicate attempt: Already eaten at {orig_mess_name}" if is_different_mess else f"Duplicate punch attempt for {meal_type}"

            # Log duplicate attempt in database for audit trail
            MessLog.objects.create(
                employee=employee,
                mess_location=mess_loc,
                date=today,
                meal_type=meal_type,
                scanned_by=request.user,
                status='DUPLICATE',
                remarks=remarks_text
            )

            shift_name = employee.get_current_shift_display() if hasattr(employee, 'get_current_shift_display') else (employee.current_shift or 'Day Shift')

            photo_url = ''
            if hasattr(employee, 'profile_picture') and getattr(employee, 'profile_picture', None):
                try:
                    photo_url = employee.profile_picture.url
                except Exception:
                    photo_url = ''

            if is_different_mess:
                alert_msg = f"⛔ FRAUD / MULTI-MESS ALERT! {employee.name} ALREADY ATE {meal_type} at '{orig_mess_name}' ({orig_time_str})! Strictly forbidden to take food again at '{curr_mess_name}'."
            else:
                alert_msg = f"⚠️ ALREADY SERVED! {employee.name} was already served {meal_type} at {orig_time_str} ({orig_mess_name})."

            return JsonResponse({
                'status': 'DUPLICATE',
                'emp_name': employee.name,
                'emp_id': employee.emp_id,
                'department': employee.department or 'General',
                'designation': employee.designation or 'Staff / Worker',
                'photo_url': photo_url,
                'shift': shift_name,
                'meal_type': meal_type,
                'meal_display': original_log.get_meal_type_display(),
                'mess_location_name': orig_mess_name,
                'current_mess_name': curr_mess_name,
                'is_different_mess': is_different_mess,
                'original_time': orig_time_str,
                'original_date': orig_date_str,
                'original_token': original_log.verification_token or f"PASS-{original_log.id:06d}",
                'current_attempt_time': now_local.strftime('%I:%M:%S %p'),
                'msg': alert_msg
            })

        import uuid
        pass_token = token_match.verification_token if token_match else f"PASS-{uuid.uuid4().hex[:8].upper()}"

        new_log = MessLog.objects.create(
            employee=employee,
            mess_location=mess_loc,
            date=today,
            meal_type=meal_type,
            scanned_by=request.user,
            status='SUCCESS',
            verification_token=pass_token,
            remarks=f"Served {meal_type}"
        )

        kpi = {
            'breakfast': MessLog.objects.filter(date=today, meal_type='BREAKFAST', status='SUCCESS').count(),
            'lunch': MessLog.objects.filter(date=today, meal_type='LUNCH', status='SUCCESS').count(),
            'dinner': MessLog.objects.filter(date=today, meal_type='DINNER', status='SUCCESS').count(),
            'total': MessLog.objects.filter(date=today, status='SUCCESS').count(),
        }

        photo_url = ''
        if hasattr(employee, 'profile_picture') and getattr(employee, 'profile_picture', None):
            try:
                photo_url = employee.profile_picture.url
            except Exception:
                photo_url = ''

        shift_name = employee.get_current_shift_display() if hasattr(employee, 'get_current_shift_display') else (employee.current_shift or 'Day Shift')
        exact_time_str = timezone.localtime(new_log.punch_time).strftime('%I:%M:%S %p')
        exact_date_str = new_log.date.strftime('%d %b %Y')
        mess_name_str = mess_loc.name if mess_loc else 'Main Canteen'

        return JsonResponse({
            'status': 'SUCCESS',
            'emp_name': employee.name,
            'emp_id': employee.emp_id,
            'department': employee.department or 'General',
            'designation': employee.designation or 'Staff / Worker',
            'shift': shift_name,
            'photo_url': photo_url,
            'meal_type': meal_type,
            'meal_display': new_log.get_meal_type_display(),
            'mess_location_name': mess_name_str,
            'token': pass_token,
            'scan_time': exact_time_str,
            'scan_date': exact_date_str,
            'kpi': kpi,
            'msg': f"🟢 MEAL ISSUED SUCCESSFULLY at {exact_time_str}"
        })

    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def mess_contractor_view(request):
    """
    Contractor Ration Requirement & Wastage Logger Hub with Date Range & Multi-Filters
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    is_authorized = (
        request.user.is_superuser or
        request.user.system_role in ['MANAGER', 'PROJECT_MANAGER'] or
        'mess_manager' in allowed_modules
    )
    if not is_authorized:
        messages.error(request, "Permission Denied: Access to Contractor Hub requires Manager permission.")
        return redirect('mess_user_view')

    today = timezone.now().date()
    today_str = today.strftime('%Y-%m-%d')
    from_date_str = request.GET.get('from_date', today_str)
    to_date_str = request.GET.get('to_date', today_str)
    selected_mess = request.GET.get('mess_id', 'ALL')
    selected_meal = request.GET.get('meal_type', 'ALL')

    try:
        from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
    except ValueError:
        from_date = today

    try:
        to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
    except ValueError:
        to_date = today

    total_active_workers = Employee.objects.filter(status='Active').count() or 100

    ration = {
        'rice_kg': round(total_active_workers * 0.20, 1),
        'atta_kg': round(total_active_workers * 0.15, 1),
        'dal_kg': round(total_active_workers * 0.10, 1),
        'veg_kg': round(total_active_workers * 0.18, 1),
    }

    mess_locations = MessLocation.objects.filter(is_active=True)

    wastage_qs = MessWastageLog.objects.filter(date__range=[from_date, to_date]).select_related('mess_location')
    if selected_mess != 'ALL' and selected_mess:
        wastage_qs = wastage_qs.filter(mess_location_id=selected_mess)
    if selected_meal != 'ALL' and selected_meal:
        wastage_qs = wastage_qs.filter(meal_type=selected_meal)

    wastage_logs = wastage_qs.order_by('-created_at')[:100]

    return render(request, 'mess_contractor.html', {
        'total_active_workers': total_active_workers,
        'ration': ration,
        'mess_locations': mess_locations,
        'wastage_logs': wastage_logs,
        'today_date': today,
        'from_date': from_date_str,
        'to_date': to_date_str,
        'selected_mess': selected_mess,
        'selected_meal': selected_meal,
    })


@csrf_exempt
@login_required
def api_log_mess_wastage(request):
    if request.method == 'POST':
        try:
            mess_id = request.POST.get('mess_location_id')
            meal_type = request.POST.get('meal_type', 'LUNCH')
            prep_kg = float(request.POST.get('prepared_qty_kg', 0) or 0)
            waste_kg = float(request.POST.get('wasted_qty_kg', 0) or 0)

            mess_loc = MessLocation.objects.filter(id=mess_id).first()

            MessWastageLog.objects.create(
                mess_location=mess_loc,
                date=timezone.now().date(),
                meal_type=meal_type,
                prepared_qty_kg=prep_kg,
                wasted_qty_kg=waste_kg,
                logged_by=request.user
            )
            messages.success(request, "Food wastage logged successfully!")
        except Exception as e:
            messages.error(request, f"Wastage log error: {str(e)}")
    return redirect('mess_contractor_view')


@login_required
@login_required
def mess_reports_view(request):
    """
    Comprehensive Mess Consumption & Attendance Ledgers:
    Detailed tracking of how many people ate (kitne logo ne khaya),
    when they ate, and in which mess location.
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    is_authorized = (
        request.user.is_superuser or
        request.user.system_role == 'MANAGER' or
        any(m in allowed_modules for m in ['mess', 'mess_manager'])
    )
    if not is_authorized:
        messages.error(request, "Permission Denied: Mess Reports are reserved for Mess Incharges & Managers.")
        return redirect('mess_user_view')

    today = timezone.now().date()
    from_date = request.GET.get('from_date', '')
    to_date = request.GET.get('to_date', '')
    search_query = request.GET.get('search', '').strip()
    selected_dept = request.GET.get('department', '').strip()
    selected_meal = request.GET.get('meal_type', 'ALL').strip()
    selected_mess = request.GET.get('mess_id', 'ALL').strip()
    selected_tk = request.GET.get('timekeeper_id', 'ALL').strip()
    selected_status = request.GET.get('status', 'ALL').strip()

    logs = MessLog.objects.select_related('employee', 'employee__assigned_mess', 'mess_location', 'scanned_by').all()

    if from_date: logs = logs.filter(date__gte=from_date)
    if to_date: logs = logs.filter(date__lte=to_date)
    if search_query:
        logs = logs.filter(
            Q(employee__name__icontains=search_query) |
            Q(employee__emp_id__icontains=search_query) |
            Q(employee__contact_info__icontains=search_query) |
            Q(employee__work_permit_no__icontains=search_query) |
            Q(verification_token__icontains=search_query)
        )
    if selected_dept and selected_dept != 'ALL': logs = logs.filter(employee__department__icontains=selected_dept)
    if selected_meal and selected_meal != 'ALL': logs = logs.filter(meal_type=selected_meal)
    if selected_mess and selected_mess != 'ALL': logs = logs.filter(mess_location_id=selected_mess)
    if selected_tk and selected_tk != 'ALL': logs = logs.filter(scanned_by_id=selected_tk)
    if selected_status and selected_status != 'ALL': logs = logs.filter(status=selected_status)

    # Master Statistics (Diners, Timelines, Locations)
    total_meals_count = logs.filter(status='SUCCESS').count()
    unique_workers_count = logs.filter(status='SUCCESS').values('employee_id').distinct().count()
    duplicate_blocked_count = logs.filter(status='DUPLICATE').count()

    bf_count = logs.filter(meal_type='BREAKFAST', status='SUCCESS').count()
    lunch_count = logs.filter(meal_type='LUNCH', status='SUCCESS').count()
    dinner_count = logs.filter(meal_type='DINNER', status='SUCCESS').count()
    night_count = logs.filter(meal_type='NIGHT_REFRESHMENT', status='SUCCESS').count()

    mess_locations = MessLocation.objects.filter(is_active=True).order_by('name')
    mess_stats = []
    for loc in mess_locations:
        cnt = logs.filter(mess_location=loc, status='SUCCESS').count()
        mess_stats.append({
            'loc': loc,
            'count': cnt,
            'contractor': loc.contractor_agency or '-'
        })

    logs_ordered = logs.order_by('-punch_time')
    departments = list(Employee.objects.values_list('department', flat=True).distinct())
    departments = [d for d in departments if d]

    scanned_by_ids = MessLog.objects.exclude(scanned_by=None).values_list('scanned_by_id', flat=True).distinct()
    timekeepers = User.objects.filter(Q(id__in=scanned_by_ids) | Q(system_role__in=['TIMEKEEPER', 'DEO', 'SCANNER', 'MANAGER'])).distinct().order_by('first_name', 'username')

    return render(request, 'mess_reports.html', {
        'logs': logs_ordered[:1000],
        'total_count': logs.count(),
        'total_meals_count': total_meals_count,
        'unique_workers_count': unique_workers_count,
        'duplicate_blocked_count': duplicate_blocked_count,
        'bf_count': bf_count,
        'lunch_count': lunch_count,
        'dinner_count': dinner_count,
        'night_count': night_count,
        'mess_stats': mess_stats,
        'from_date': from_date or '',
        'to_date': to_date or '',
        'search_query': search_query,
        'selected_dept': selected_dept,
        'selected_meal': selected_meal,
        'selected_mess': selected_mess,
        'selected_tk': selected_tk,
        'selected_status': selected_status,
        'departments': departments,
        'mess_locations': mess_locations,
        'timekeepers': timekeepers,
        'today_date': today,
    })


@login_required
def export_mess_excel(request):
    """
    Excel Export with complete granular details:
    Who ate, when they ate, and in which mess location.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from django.http import HttpResponse

    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    search_query = request.GET.get('search', '').strip()
    selected_dept = request.GET.get('department', '').strip()
    selected_meal = request.GET.get('meal_type', 'ALL').strip()
    selected_mess = request.GET.get('mess_id', 'ALL').strip()
    selected_tk = request.GET.get('timekeeper_id', 'ALL').strip()
    selected_status = request.GET.get('status', 'ALL').strip()

    logs = MessLog.objects.select_related('employee', 'employee__assigned_mess', 'mess_location', 'scanned_by').all()

    if from_date: logs = logs.filter(date__gte=from_date)
    if to_date: logs = logs.filter(date__lte=to_date)
    if search_query:
        logs = logs.filter(
            Q(employee__name__icontains=search_query) |
            Q(employee__emp_id__icontains=search_query) |
            Q(employee__contact_info__icontains=search_query) |
            Q(employee__work_permit_no__icontains=search_query) |
            Q(verification_token__icontains=search_query)
        )
    if selected_dept and selected_dept != 'ALL': logs = logs.filter(employee__department__icontains=selected_dept)
    if selected_meal and selected_meal != 'ALL': logs = logs.filter(meal_type=selected_meal)
    if selected_mess and selected_mess != 'ALL': logs = logs.filter(mess_location_id=selected_mess)
    if selected_tk and selected_tk != 'ALL': logs = logs.filter(scanned_by_id=selected_tk)
    if selected_status and selected_status != 'ALL': logs = logs.filter(status=selected_status)

    logs = logs.order_by('-punch_time')[:5000]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detailed Meal Ledgers"

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    headers = [
        "SL NO", "DATE", "TIME (EXACT PUNCH)", "MEAL WINDOW",
        "MESS LOCATION", "CATERING CONTRACTOR", "EMPLOYEE ID",
        "WORKER NAME", "MOBILE / PHONE NUMBER", "WORK PERMIT NUMBER",
        "DESIGNATION / POST", "DEPARTMENT", "ASSIGNED MESS", "GUEST / DIFFERENT MESS?",
        "VERIFICATION TOKEN", "SCANNER / OPERATOR", "ENTRY MODE", "STATUS", "REMARKS"
    ]
    ws.append(headers)

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for idx, l in enumerate(logs, 1):
        emp_code = f"EMP-{l.employee.id:04d}" if '@' in l.employee.emp_id else l.employee.emp_id
        desig = l.employee.designation or "Staff / Worker"
        phone = l.employee.contact_info or '-'
        permit = l.employee.work_permit_no or '-'
        mess_name = l.mess_location.name if l.mess_location else "Main Canteen"
        assigned_mess_name = l.employee.assigned_mess.name if l.employee.assigned_mess else "Unassigned"
        is_diff_mess = "YES (GUEST ENTRY)" if (l.employee.assigned_mess and l.mess_location and l.employee.assigned_mess.id != l.mess_location.id) else "NO"
        contractor = l.mess_location.contractor_agency if (l.mess_location and l.mess_location.contractor_agency) else "-"
        tk_name = (l.scanned_by.get_full_name() or l.scanned_by.username) if l.scanned_by else "Self Scan / Wall QR"
        punch_time_12hr = timezone.localtime(l.punch_time).strftime('%I:%M:%S %p') if l.punch_time else '-'

        ws.append([
            idx,
            l.date.strftime('%Y-%m-%d') if l.date else '-',
            punch_time_12hr,
            l.get_meal_type_display(),
            mess_name,
            contractor,
            emp_code,
            l.employee.name,
            phone,
            permit,
            desig,
            l.employee.department or '-',
            assigned_mess_name,
            is_diff_mess,
            l.verification_token or '-',
            tk_name,
            l.entry_mode or 'SCANNER',
            l.status,
            l.remarks or ''
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=mess_detailed_consumption_report.xlsx'
    wb.save(response)
    return response


@login_required
def export_mess_pdf(request):
    """
    Executive Printable Landscape PDF Report Engine for Mess Meal Consumption.
    Supports granular filters:
    - Scope: 'all' (entire history) or date range (from_date / to_date)
    - Mess Facility: mess_id (ALL or specific)
    - Meal Window: meal_type (ALL, BREAKFAST, LUNCH, DINNER, SNACKS)
    - Status: status (ALL, SUCCESS, DUPLICATE)
    - Department: department
    - Search: search text
    """
    from django.http import HttpResponse
    from django.utils import timezone
    from django.db.models import Q

    scope = request.GET.get('scope', '').strip().lower()
    from_date = request.GET.get('from_date', '').strip()
    to_date = request.GET.get('to_date', '').strip()
    selected_dept = request.GET.get('department', '').strip()
    selected_meal = request.GET.get('meal_type', 'ALL').strip().upper()
    selected_mess = request.GET.get('mess_id', 'ALL').strip()
    selected_status = request.GET.get('status', 'ALL').strip()
    search_query = request.GET.get('search', '').strip()

    logs = MessLog.objects.select_related('employee', 'employee__assigned_mess', 'mess_location', 'scanned_by').all()

    if scope != 'all':
        if from_date: logs = logs.filter(date__gte=from_date)
        if to_date: logs = logs.filter(date__lte=to_date)
    else:
        from_date = ''
        to_date = ''

    if selected_dept and selected_dept != 'ALL':
        logs = logs.filter(employee__department__icontains=selected_dept)
    if selected_meal and selected_meal != 'ALL':
        logs = logs.filter(meal_type=selected_meal)
    if selected_mess and selected_mess != 'ALL':
        logs = logs.filter(mess_location_id=selected_mess)
    if selected_status and selected_status != 'ALL':
        logs = logs.filter(status=selected_status)
    if search_query:
        logs = logs.filter(
            Q(employee__name__icontains=search_query) |
            Q(employee__emp_id__icontains=search_query) |
            Q(employee__contact_info__icontains=search_query) |
            Q(employee__work_permit_no__icontains=search_query) |
            Q(verification_token__icontains=search_query)
        )

    # Compute KPI Summaries for Header
    total_records = logs.count()
    total_served = logs.filter(status='SUCCESS').count()
    unique_diners = logs.filter(status='SUCCESS').values('employee_id').distinct().count()
    bf_count = logs.filter(meal_type='BREAKFAST', status='SUCCESS').count()
    lunch_count = logs.filter(meal_type='LUNCH', status='SUCCESS').count()
    dinner_count = logs.filter(meal_type='DINNER', status='SUCCESS').count()
    dup_count = logs.filter(status='DUPLICATE').count()

    # Filter Label Text
    date_filter_label = f"{from_date} to {to_date}" if (from_date and to_date) else (f"From {from_date}" if from_date else (f"Until {to_date}" if to_date else "All Dates (Entire History)"))
    
    mess_filter_label = "All Mess Facilities"
    if selected_mess and selected_mess != 'ALL':
        m_obj = MessLocation.objects.filter(id=selected_mess).first()
        if m_obj:
            mess_filter_label = m_obj.name

    meal_filter_label = "All Meal Windows" if selected_meal == 'ALL' else selected_meal.replace('_', ' ').title()
    status_filter_label = "All Status" if selected_status == 'ALL' else selected_status.title()

    logs = logs.order_by('-date', '-punch_time')[:3000]

    rows_html = []
    for idx, l in enumerate(logs, 1):
        emp_code = f"EMP-{l.employee.id:04d}" if '@' in l.employee.emp_id else l.employee.emp_id
        emp_name = l.employee.name or "Unknown Worker"
        desig = l.employee.designation or l.employee.post or "Worker / Staff"
        phone = l.employee.contact_info or '-'
        permit = l.employee.work_permit_no or '-'
        mess_name = l.mess_location.name if l.mess_location else "Main Canteen"
        assigned_mess_name = l.employee.assigned_mess.name if l.employee.assigned_mess else "-"
        tk_name = (l.scanned_by.get_full_name() or l.scanned_by.username) if l.scanned_by else "Wall QR Scan"
        punch_time_str = timezone.localtime(l.punch_time).strftime('%I:%M:%S %p') if l.punch_time else '-'
        date_str = l.date.strftime('%d %b %Y') if l.date else '-'

        meal_badge_color = '#ea580c' if l.meal_type == 'LUNCH' else ('#d97706' if l.meal_type == 'BREAKFAST' else '#7c3aed')
        status_color = '#16a34a' if l.status == 'SUCCESS' else '#dc2626'
        status_text = '🟢 Served' if l.status == 'SUCCESS' else '🔴 Duplicate Blocked'

        rows_html.append(f"""
        <tr style="border-bottom: 1px solid #e2e8f0; font-size: 11px;">
            <td style="padding: 6px 8px; text-align: center; color: #64748b; font-weight: bold;">{idx}</td>
            <td style="padding: 6px 8px; white-space: nowrap;">
                <b>{date_str}</b><br>
                <span style="color: #0284c7; font-family: monospace; font-size: 10px;">⏱️ {punch_time_str}</span>
            </td>
            <td style="padding: 6px 8px; font-weight: bold; color: {meal_badge_color};">
                {l.get_meal_type_display()}
            </td>
            <td style="padding: 6px 8px; font-weight: bold; color: #0d9488;">
                🏢 {mess_name}
            </td>
            <td style="padding: 6px 8px;">
                <b style="color: #0f172a;">{emp_name}</b><br>
                <span style="color: #2563eb; font-weight: bold;">ID: {emp_code}</span>
                {f'<span style="color:#64748b;"> &bull; 📞 {phone}</span>' if phone != '-' else ''}
            </td>
            <td style="padding: 6px 8px;">
                {l.employee.department or 'Site'}<br>
                <small style="color: #64748b;">{desig}</small>
            </td>
            <td style="padding: 6px 8px; color: #475569;">
                {assigned_mess_name}
            </td>
            <td style="padding: 6px 8px; font-family: monospace; font-weight: bold; color: #1e293b;">
                {l.verification_token or '-'}
            </td>
            <td style="padding: 6px 8px; font-size: 10px; color: #64748b;">
                {l.entry_mode or 'QR_SCAN'}<br>By: {tk_name}
            </td>
            <td style="padding: 6px 8px; color: {status_color}; font-weight: bold; white-space: nowrap;">
                {status_text}
            </td>
        </tr>
        """)

    generated_time_str = timezone.localtime(timezone.now()).strftime('%d %b %Y, %I:%M:%S %p')

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Mess Meal Consumption Report - {date_filter_label}</title>
    <style>
        @page {{
            size: landscape;
            margin: 8mm;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 10px;
            color: #0f172a;
            background: #ffffff;
        }}
        .no-print {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #0f172a;
            color: white;
            padding: 12px 18px;
            border-radius: 10px;
            margin-bottom: 16px;
        }}
        .no-print button, .no-print a {{
            background: #2563eb;
            color: white;
            font-weight: bold;
            padding: 8px 16px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            text-decoration: none;
            font-size: 13px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .report-header {{
            border-bottom: 3px solid #0f172a;
            padding-bottom: 10px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
        }}
        .filter-statement {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 8px 14px;
            margin-bottom: 12px;
            font-size: 11px;
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
        }}
        .kpi-row {{
            display: flex;
            gap: 10px;
            margin-bottom: 14px;
        }}
        .kpi-card {{
            flex: 1;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 8px 10px;
            text-align: center;
        }}
        .kpi-num {{
            font-size: 18px;
            font-weight: 900;
            margin-top: 2px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 11px;
        }}
        th {{
            background: #0f172a;
            color: white;
            padding: 8px 6px;
            text-align: left;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        tr:nth-child(even) {{
            background: #f8fafc;
        }}
        @media print {{
            .no-print {{ display: none !important; }}
            body {{ padding: 0; }}
        }}
    </style>
</head>
<body>

    <!-- TOP PRINT BAR (Hidden when printed) -->
    <div class="no-print">
        <div style="font-weight: bold; font-size: 14px;">
            📄 Mess Meal Consumption Report Preview &bull; {total_records} Records
        </div>
        <div style="display: flex; gap: 10px;">
            <button onclick="window.print()" style="background: #10b981;">
                🖨️ Print / Save as PDF
            </button>
            <a href="/mess/export-excel/?from_date={from_date}&to_date={to_date}&mess_id={selected_mess}&meal_type={selected_meal}&status={selected_status}&department={selected_dept}" style="background: #059669;">
                📥 Export Excel (.xlsx)
            </a>
            <button onclick="window.close()" style="background: #475569;">
                ✕ Close
            </button>
        </div>
    </div>

    <!-- REPORT HEADER -->
    <div class="report-header">
        <div>
            <div style="font-size: 12px; font-weight: 800; text-transform: uppercase; color: #2563eb; letter-spacing: 1px;">
                Canteen Operations &amp; Feeding Statements
            </div>
            <h1 style="margin: 3px 0 0 0; font-size: 20px; font-weight: 900; color: #0f172a;">
                🍱 Mess Meal Consumption &amp; Headcount Audit Report
            </h1>
        </div>
        <div style="text-align: right; font-size: 10px; color: #64748b;">
            <div><b>Generated:</b> {generated_time_str}</div>
            <div><b>Total Punches:</b> {total_records} Records</div>
        </div>
    </div>

    <!-- FILTER PARAMETERS APPLIED -->
    <div class="filter-statement">
        <div><b>📅 Date Range:</b> <span style="color: #2563eb;">{date_filter_label}</span></div>
        <div><b>🏢 Mess Facility:</b> <span style="color: #0d9488;">{mess_filter_label}</span></div>
        <div><b>🍽️ Meal Window:</b> <span style="color: #d97706;">{meal_filter_label}</span></div>
        <div><b>Status:</b> <span>{status_filter_label}</span></div>
        {f'<div><b>Department:</b> <span>{selected_dept}</span></div>' if selected_dept and selected_dept != 'ALL' else ''}
    </div>

    <!-- KPI SUMMARY BAR -->
    <div class="kpi-row">
        <div class="kpi-card" style="border-top: 3px solid #2563eb;">
            <div style="font-size: 10px; font-weight: bold; color: #64748b;">TOTAL MEALS SERVED</div>
            <div class="kpi-num" style="color: #1e3a8a;">{total_served}</div>
        </div>
        <div class="kpi-card" style="border-top: 3px solid #10b981;">
            <div style="font-size: 10px; font-weight: bold; color: #64748b;">UNIQUE DINERS</div>
            <div class="kpi-num" style="color: #065f46;">{unique_diners}</div>
        </div>
        <div class="kpi-card" style="border-top: 3px solid #f59e0b;">
            <div style="font-size: 10px; font-weight: bold; color: #64748b;">BREAKFAST</div>
            <div class="kpi-num" style="color: #92400e;">{bf_count}</div>
        </div>
        <div class="kpi-card" style="border-top: 3px solid #ea580c;">
            <div style="font-size: 10px; font-weight: bold; color: #64748b;">LUNCH</div>
            <div class="kpi-num" style="color: #9a3412;">{lunch_count}</div>
        </div>
        <div class="kpi-card" style="border-top: 3px solid #8b5cf6;">
            <div style="font-size: 10px; font-weight: bold; color: #64748b;">DINNER</div>
            <div class="kpi-num" style="color: #5b21b6;">{dinner_count}</div>
        </div>
        <div class="kpi-card" style="border-top: 3px solid #ef4444;">
            <div style="font-size: 10px; font-weight: bold; color: #64748b;">DUPLICATES BLOCKED</div>
            <div class="kpi-num" style="color: #991b1b;">{dup_count}</div>
        </div>
    </div>

    <!-- MAIN DATA TABLE -->
    <table>
        <thead>
            <tr>
                <th style="width: 25px; text-align: center;">#</th>
                <th>Date &amp; Time</th>
                <th>Meal Window</th>
                <th>Mess Facility</th>
                <th>Worker Details</th>
                <th>Department &amp; Post</th>
                <th>Assigned Mess</th>
                <th>Pass Token</th>
                <th>Mode / Operator</th>
                <th>Status</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows_html) if rows_html else '<tr><td colspan="10" style="text-align:center; padding:20px;">No consumption records found for the selected criteria.</td></tr>'}
        </tbody>
    </table>

    <div style="margin-top: 14px; display: flex; justify-content: space-between; font-size: 10px; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 8px;">
        <div>Executive Mess Management System &bull; Confidential Meal Audit Record</div>
        <div>Page 1 of 1</div>
    </div>

</body>
</html>"""
    return HttpResponse(html)


@csrf_exempt
@login_required
def api_add_mess_location(request):
    """
    API to create or edit a Mess Location (e.g. Mess D, Site 3)
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        mess_id = data.get('id') or data.get('mess_id')
        name = data.get('name', '').strip()
        code = data.get('code', '').strip().upper() or name.upper().replace(' ', '_')[:20]
        capacity = int(data.get('capacity', 200))
        contractor = data.get('contractor_agency', '').strip()

        if not name:
            return JsonResponse({'status': 'ERROR', 'msg': 'Mess Location Name is required.'}, status=400)

        if mess_id:
            loc = MessLocation.objects.filter(id=int(mess_id)).first()
            if not loc:
                return JsonResponse({'status': 'ERROR', 'msg': 'Mess Location not found.'}, status=404)
            # Check duplicate name/code on other locations
            if MessLocation.objects.filter(name__iexact=name).exclude(id=loc.id).exists():
                return JsonResponse({'status': 'ERROR', 'msg': f'Another Mess with name "{name}" already exists.'}, status=400)
            if MessLocation.objects.filter(code__iexact=code).exclude(id=loc.id).exists():
                return JsonResponse({'status': 'ERROR', 'msg': f'Another Mess with code "{code}" already exists.'}, status=400)
            loc.name = name
            loc.code = code
            loc.capacity = capacity
            loc.contractor_agency = contractor
            loc.is_active = True
            loc.save()
            return JsonResponse({'status': 'SUCCESS', 'msg': f'Mess Location "{name}" updated successfully!'})

        # New Mess Location
        if MessLocation.objects.filter(name__iexact=name).exists():
            return JsonResponse({'status': 'ERROR', 'msg': f'Mess with name "{name}" already exists.'}, status=400)
        if MessLocation.objects.filter(code__iexact=code).exists():
            return JsonResponse({'status': 'ERROR', 'msg': f'Mess with code "{code}" already exists.'}, status=400)

        MessLocation.objects.create(
            name=name,
            code=code,
            capacity=capacity,
            contractor_agency=contractor,
            is_active=True
        )

        return JsonResponse({'status': 'SUCCESS', 'msg': f'Mess Location "{name}" added successfully!'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_delete_mess_location(request):
    """
    API to soft-delete or remove a Mess Location safely.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        mess_id = data.get('id') or data.get('mess_id')
        if not mess_id:
            return JsonResponse({'status': 'ERROR', 'msg': 'Mess Location ID is required.'}, status=400)

        loc = MessLocation.objects.filter(id=int(mess_id)).first()
        if not loc:
            return JsonResponse({'status': 'ERROR', 'msg': 'Mess Location not found.'}, status=404)

        # Count references
        emp_assigned_count = Employee.objects.filter(assigned_mess=loc).count()
        logs_count = MessLog.objects.filter(mess_location=loc).count()

        name = loc.name
        # Soft delete by setting is_active=False and unassigning active pointers
        Employee.objects.filter(assigned_mess=loc).update(assigned_mess=None)
        loc.is_active = False
        loc.save()

        # If no historical logs exist, can be safely deleted completely
        if logs_count == 0:
            loc.delete()
            return JsonResponse({'status': 'SUCCESS', 'msg': f'Mess Location "{name}" deleted successfully!'})

        return JsonResponse({'status': 'SUCCESS', 'msg': f'Mess Location "{name}" archived/deactivated successfully ({logs_count} past logs preserved)!'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)



@csrf_exempt
@login_required
def api_add_timekeeper(request):
    """
    API to create/assign a Timekeeper / Scanner Operator account.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        username = data.get('username', '').strip()
        full_name = data.get('full_name', '').strip()
        password = data.get('password', '').strip() or 'timekeeper123'
        
        if not username:
            return JsonResponse({'status': 'ERROR', 'msg': 'Timekeeper Username is required.'}, status=400)

        if User.objects.filter(username=username).exists():
            return JsonResponse({'status': 'ERROR', 'msg': f'Username "{username}" already exists!'}, status=400)

        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=full_name,
            system_role='SCANNER'
        )
        
        assigned_mods = getattr(user, 'assigned_modules', []) or []
        if 'mess_scanner' not in assigned_mods:
            assigned_mods.append('mess_scanner')
            user.assigned_modules = assigned_mods
            user.save()

        return JsonResponse({'status': 'SUCCESS', 'msg': f'Timekeeper account "{username}" created successfully with password "{password}"!'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def api_search_employees(request):
    """
    API for live employee search dropdown.
    """
    q = request.GET.get('q', '').strip()
    if not q:
        employees = Employee.objects.filter(status='Active')[:20]
    else:
        employees = Employee.objects.filter(status='Active').filter(
            Q(name__icontains=q) | Q(emp_id__icontains=q)
        )[:30]

    results = [{'id': emp.id, 'emp_id': emp.emp_id, 'name': emp.name, 'department': emp.department or 'General', 'phone': emp.contact_info or ''} for emp in employees]
    return JsonResponse({'status': 'SUCCESS', 'employees': results})


@csrf_exempt
@login_required
def api_assign_employee_mess(request):
    """
    API to assign one or multiple employees to a specific Mess Location.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        employee_ids = data.get('employee_ids', [])
        mess_id = data.get('mess_id')

        if not employee_ids:
            return JsonResponse({'status': 'ERROR', 'msg': 'No employees selected.'}, status=400)

        mess_loc = None
        if mess_id and str(mess_id) != 'UNASSIGN':
            mess_loc = MessLocation.objects.filter(id=mess_id).first()

        updated_count = Employee.objects.filter(id__in=employee_ids).update(assigned_mess=mess_loc)

        mess_name = mess_loc.name if mess_loc else "Unassigned"
        return JsonResponse({'status': 'SUCCESS', 'msg': f'Successfully assigned {updated_count} employee(s) to "{mess_name}"!'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_quick_create_and_assign_worker(request):
    """
    API to quickly register a new Employee/Worker on the spot and assign them to a Mess Location.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body)
        name = str(data.get('name', '')).strip()
        emp_id = str(data.get('emp_id', '')).strip().upper()
        phone = str(data.get('phone', '')).strip()
        department = str(data.get('department', 'General')).strip()
        designation = str(data.get('designation', 'Staff / Worker')).strip()
        mess_id = data.get('mess_id')

        if not name:
            return JsonResponse({'status': 'ERROR', 'msg': 'Worker Full Name is required.'}, status=400)

        # Generate Emp ID if not supplied
        if not emp_id:
            emp_id = f"EMP-{Employee.objects.count() + 1001:04d}"

        # Check existing
        existing = Employee.objects.filter(Q(emp_id__iexact=emp_id) | (Q(contact_info=phone) if phone else Q(pk=None))).first()
        mess_loc = None
        if mess_id and str(mess_id) != 'UNASSIGN':
            mess_loc = MessLocation.objects.filter(id=int(mess_id)).first()

        if existing:
            existing.name = name or existing.name
            if phone: existing.contact_info = phone
            if department: existing.department = department
            if designation: existing.designation = designation
            existing.assigned_mess = mess_loc
            existing.status = 'Active'
            existing.save()
            mess_name = mess_loc.name if mess_loc else "Unassigned"
            return JsonResponse({
                'status': 'SUCCESS',
                'msg': f'Existing Employee "{existing.name}" ({existing.emp_id}) updated and assigned to "{mess_name}"!',
                'employee': {'id': existing.id, 'emp_id': existing.emp_id, 'name': existing.name, 'mess': mess_name}
            })

        emp = Employee.objects.create(
            emp_id=emp_id,
            name=name,
            contact_info=phone,
            department=department or 'General',
            designation=designation or 'Staff / Worker',
            assigned_mess=mess_loc,
            status='Active'
        )

        mess_name = mess_loc.name if mess_loc else "Unassigned"
        return JsonResponse({
            'status': 'SUCCESS',
            'msg': f'New Employee "{emp.name}" ({emp.emp_id}) registered and assigned to "{mess_name}"!',
            'employee': {'id': emp.id, 'emp_id': emp.emp_id, 'name': emp.name, 'mess': mess_name}
        })
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)



@csrf_exempt
@login_required
def api_sync_desktop_employees(request):
    """
    API endpoint to trigger syncing from Desktop Excel sheet (Employee (3).xlsx).
    """
    try:
        from django.core.management import call_command
        call_command('sync_desktop_employees')
        total_count = Employee.objects.count()
        return JsonResponse({'status': 'SUCCESS', 'msg': f'Successfully synced with Desktop Excel sheet! Total Employees: {total_count}'})
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def mess_qr_poster_view(request, mess_id):
    """
    Renders printable QR Code Poster view for a specific Mess Location.
    Only accessible by Managers or Superusers.
    """
    mess_loc = get_object_or_404(MessLocation, id=mess_id)
    domain = request.build_absolute_uri('/')[:-1]
    claim_url = f"{domain}/mess/claim-pass/{mess_loc.id}/"
    return render(request, 'mess_qr_poster.html', {
        'mess_location': mess_loc,
        'claim_url': claim_url,
    })


@csrf_exempt
def mess_claim_pass_view(request, mess_id):
    """
    View for employees scanning Mess Wall QR Code to claim their Meal Pass.
    - If unauthenticated: Redirects to Mess Login / Registration with ?claim_mess=<mess_id>
    - If authenticated: Automatically claims pass for current meal window at this mess and opens user portal (/mess/user/)
    """
    mess_loc = get_object_or_404(MessLocation, id=mess_id)
    
    if not request.user.is_authenticated:
        return redirect(f"{reverse('mess_login_view')}?claim_mess={mess_loc.id}")

    employee = _get_or_create_user_employee(request.user)
    now_local = timezone.localtime(timezone.now())
    today = now_local.date()
    active_meal, meal_timing, _ = _get_active_meal_window_and_timing(employee)

    existing = MessLog.objects.filter(
        employee=employee,
        date=today,
        meal_type=active_meal,
        status='SUCCESS'
    ).first()

    if existing:
        orig_mess_name = existing.mess_location.name if existing.mess_location else mess_loc.name
        is_diff_mess = bool(existing.mess_location and existing.mess_location.id != mess_loc.id)

        if is_diff_mess:
            remarks_text = f"MULTI-MESS duplicate claim: Already eaten at {orig_mess_name}"
            MessLog.objects.create(
                employee=employee,
                mess_location=mess_loc,
                date=today,
                meal_type=active_meal,
                scanned_by=request.user,
                status='DUPLICATE',
                remarks=remarks_text
            )
            messages.error(request, f"⛔ MULTI-MESS FRAUD BLOCKED! {employee.name}, you have already claimed {active_meal} at '{orig_mess_name}'! You cannot take meals from multiple mess locations.")
        else:
            messages.info(request, f"ℹ️ {employee.name}, your {active_meal} pass is already active ({existing.verification_token}).")
    else:
        import uuid
        token = f"PASS-{uuid.uuid4().hex[:8].upper()}"
        MessLog.objects.create(
            employee=employee,
            mess_location=mess_loc,
            date=today,
            meal_type=active_meal,
            scanned_by=request.user,
            status='SUCCESS',
            entry_mode='WALL_QR_CLAIM',
            verification_token=token,
            remarks=f"Wall QR Pass claimed at {mess_loc.name}"
        )
        messages.success(request, f"✅ Meal Pass Confirmed! {employee.name}, your {active_meal} pass has been successfully activated at {mess_loc.name}.")

    return redirect('mess_user_view')


# ============================================================
# CAMP MANAGEMENT SYSTEM - SECURITY GATE, HR ROSTER & DASHBOARD
# ============================================================

@login_required
def camp_root_view(request):
    """
    Root router for /camp/ based on user's assigned role & modules:
    - Guard -> /camp/gate/
    - HR -> /camp/hr/
    - Cook -> /mess/kitchen/
    - Scanner -> /mess/scan/
    - Manager / PM -> /camp/dashboard/
    - Normal Worker -> /camp/user/
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    has_manager = 'camp_manager' in allowed_modules or 'mess_manager' in allowed_modules
    has_guard = 'camp_guard' in allowed_modules
    has_hr = 'camp_hr' in allowed_modules
    has_cook = 'mess_cook' in allowed_modules
    has_scanner = 'mess_scanner' in allowed_modules

    if has_guard:
        return redirect('camp_gate_view')
    elif has_hr:
        return redirect('camp_hr_view')
    elif has_cook:
        return redirect('mess_kitchen_view')
    elif has_scanner:
        return redirect('mess_scan_view')
    elif has_manager or request.user.is_superuser or request.user.system_role in ['MANAGER', 'PROJECT_MANAGER']:
        return redirect('camp_dashboard_view')
    else:
        return redirect('camp_user_view')


def _get_camp_curfew_and_predictive_data(camp_setting=None):
    from portal.models import CampSetting
    if not camp_setting:
        camp_setting, _ = CampSetting.objects.get_or_create(id=1)

    outside_workers = Employee.objects.filter(status='Active', camp_status='OUTSIDE')
    overstay_list = []
    now = timezone.now()
    now_time = timezone.localtime(now).time()
    curfew_start = camp_setting.curfew_start_time or dt_time(21, 30)
    max_hours = camp_setting.curfew_max_outside_hours or 4
    is_curfew_hours = (now_time >= curfew_start or now_time < dt_time(5, 0))

    outside_emp_ids = list(outside_workers.values_list('id', flat=True))
    latest_out_punches = {}
    if outside_emp_ids:
        logs = CampMovementLog.objects.filter(employee_id__in=outside_emp_ids, direction='OUT').order_by('employee_id', '-timestamp')
        for log in logs:
            if log.employee_id not in latest_out_punches:
                latest_out_punches[log.employee_id] = log

    for w in outside_workers:
        last_out = latest_out_punches.get(w.id)
        emp_code = f"EMP-{w.id:04d}" if '@' in (w.emp_id or '') else (w.emp_id or f"EMP-{w.id:04d}")

        if last_out:
            duration_secs = (now - last_out.timestamp).total_seconds()
            duration_hrs = round(duration_secs / 3600.0, 1)
            is_overdue = (duration_hrs >= max_hours)

            if is_overdue or is_curfew_hours:
                reason = f"Night Curfew (Past {curfew_start.strftime('%I:%M %p')})" if is_curfew_hours else f"Outside > {max_hours} Hours"
                overstay_list.append({
                    'id': w.id,
                    'log_id': last_out.id if last_out else None,
                    'emp_id': emp_code,
                    'name': w.name,
                    'contact_info': w.contact_info or 'N/A',
                    'department': w.department or 'General',
                    'designation': w.designation or 'Staff',
                    'out_time': last_out.timestamp.strftime('%I:%M %p'),
                    'out_date': last_out.date.strftime('%d %b') if last_out.date else '',
                    'out_date_raw': last_out.date.strftime('%Y-%m-%d') if last_out.date else '',
                    'out_time_raw': last_out.timestamp.strftime('%H:%M') if last_out.timestamp else '',
                    'duration_hours': duration_hrs,
                    'gate_name': last_out.gate_name or 'Camp Gate',
                    'purpose': last_out.get_purpose_display() if hasattr(last_out, 'get_purpose_display') else last_out.purpose,
                    'purpose_raw': last_out.purpose or 'PERSONAL',
                    'remarks': last_out.remarks or '',
                    'reason': reason,
                    'is_curfew': is_curfew_hours
                })
        else:
            if is_curfew_hours:
                overstay_list.append({
                    'id': w.id,
                    'log_id': None,
                    'emp_id': emp_code,
                    'name': w.name,
                    'contact_info': w.contact_info or 'N/A',
                    'department': w.department or 'General',
                    'designation': w.designation or 'Staff',
                    'out_time': 'Not Recorded',
                    'out_date': '',
                    'out_date_raw': '',
                    'out_time_raw': '',
                    'duration_hours': 0,
                    'gate_name': 'Unknown',
                    'purpose': 'Outside',
                    'purpose_raw': 'PERSONAL',
                    'remarks': '',
                    'reason': f"Night Curfew (Past {curfew_start.strftime('%I:%M %p')})",
                    'is_curfew': True
                })

    overstay_list.sort(key=lambda x: x['duration_hours'], reverse=True)

    inside_count = Employee.objects.filter(status='Active', camp_status='INSIDE').count()
    outside_count = Employee.objects.filter(status='Active', camp_status='OUTSIDE').count()
    buffer = max(10, int(inside_count * 0.05)) if inside_count > 0 else 0
    total_projected = inside_count + buffer
    active_meal, timing_text, _ = _get_active_meal_window_and_timing()

    # Per-Mess Location Breakdown for Managers, Cooks, and Executives
    from portal.models import MessLocation
    messes = list(MessLocation.objects.all())
    mess_breakdown = []
    for m in messes:
        m_assigned = Employee.objects.filter(assigned_mess=m, status='Active')
        m_inside = m_assigned.filter(camp_status='INSIDE').count()
        m_outside = m_assigned.filter(camp_status='OUTSIDE').count()
        m_assigned_total = m_assigned.count()
        m_buffer = max(1, int(m_inside * 0.05)) if m_inside > 0 else 0
        m_expected = m_inside + m_buffer
        mess_breakdown.append({
            'id': m.id,
            'name': m.name,
            'capacity': m.capacity,
            'assigned': m_assigned_total,
            'inside': m_inside,
            'outside': m_outside,
            'buffer': m_buffer,
            'expected_thali': m_expected,
        })

    # Unassigned workers
    unassigned_qs = Employee.objects.filter(assigned_mess=None, status='Active')
    u_assigned = unassigned_qs.count()
    u_inside = unassigned_qs.filter(camp_status='INSIDE').count()
    u_outside = unassigned_qs.filter(camp_status='OUTSIDE').count()
    u_buffer = max(5, int(u_inside * 0.05)) if u_inside > 0 else 0
    u_expected = u_inside + u_buffer
    if u_assigned > 0:
        mess_breakdown.append({
            'id': 0,
            'name': 'General / Unassigned Camp Mess',
            'capacity': '-',
            'assigned': u_assigned,
            'inside': u_inside,
            'outside': u_outside,
            'buffer': u_buffer,
            'expected_thali': u_expected,
        })

    predictive_meals = {
        'inside_headcount': inside_count,
        'outside_headcount': outside_count,
        'buffer': buffer,
        'expected_thali': total_projected,
        'active_meal': active_meal,
        'meal_timing': timing_text,
        'breakdown': mess_breakdown,
    }

    return overstay_list, predictive_meals


def _is_camp_manager(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    role = (getattr(user, 'system_role', '') or '').upper()
    if role in ['MANAGER', 'PROJECT_MANAGER', 'ADMIN']:
        return True
    modules = getattr(user, 'assigned_modules', []) or []
    if any(m in modules for m in ['camp_manager', 'manager', 'all']):
        return True
    return False


def _categorize_trade(designation):
    des = (designation or '').strip().lower()
    if any(k in des for k in ['driver', 'hvd', 'lvd', 'tipper', 'scania', 'bus', 'transit mixer', 'tm driver']):
        return 'Driver'
    elif any(k in des for k in ['operator', 'roller', 'grader', 'excavator', 'loader', 'bulldozer', 'crane', 'boom', 'plant op', 'batching']):
        return 'Operator'
    elif any(k in des for k in ['mechanic', 'electrician', 'welder', 'fitter', 'tyre', 'denter', 'technician', 'auto elec', 'auto m']):
        return 'Mechanic / Tech'
    elif any(k in des for k in ['supervisor', 'foreman', 'engineer', 'incharge', 'manager', 'officer', 'billing', 'store', 'surveyor', 'asst']):
        return 'Supervisor / Staff'
    elif any(k in des for k in ['security', 'guard', 'desuup', 'desuung', 'traffic marshall']):
        return 'Security'
    elif any(k in des for k in ['cook', 'mess', 'canteen', 'kitchen', 'mess boy', 'tea maker']):
        return 'Mess / Kitchen'
    else:
        return 'Labour / Helper'


def _natural_sort_key(s):
    import re
    if s is None:
        return []
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]


@login_required
def camp_dashboard_view(request):
    """
    Executive Camp Management Dashboard for PM Sir & Camp Managers.
    Displays live headcount (Inside vs Outside vs Leave), Gate movements, and Mess status.
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    is_authorized = (
        request.user.is_superuser or
        request.user.system_role in ['MANAGER', 'PROJECT_MANAGER'] or
        any(m in allowed_modules for m in ['camp', 'camp_manager', 'mess', 'mess_manager'])
    )
    if not is_authorized:
        messages.error(request, "Permission Denied: Camp Executive Dashboard is restricted.")
        return redirect('dashboard')

    today = timezone.now().date()

    from portal.models import (
        CampSetting, CampBlock, CampRoom, CampAssetAllotment, MessScanLog, MessLocation
    )
    camp_setting, _ = CampSetting.objects.get_or_create(id=1)
    overstay_list, predictive_meals = _get_camp_curfew_and_predictive_data(camp_setting)

    # Headcount metrics (Scoped to authentic camp & mess workforce)
    camp_workforce_qs = Employee.objects.filter(
        Q(camp_status='INSIDE') | Q(camp_room__in=['Outside', 'Private Camp']) | Q(assigned_mess__isnull=False)
    )
    total_residents = camp_workforce_qs.count()
    active_residents = camp_workforce_qs.filter(status='Active').count()
    inside_camp = camp_workforce_qs.filter(status='Active', camp_status='INSIDE').count()
    outside_camp = camp_workforce_qs.filter(status='Active', camp_status='OUTSIDE').count()
    on_leave = camp_workforce_qs.filter(status='On Leave').count()
    exited_workers = camp_workforce_qs.filter(status='Terminated').count()

    # Nationality & Category Breakdown
    expats_count = camp_workforce_qs.filter(nationality__icontains='Indian').count()
    nationals_count = camp_workforce_qs.filter(nationality__icontains='Bhutan').count()
    desuup_count = camp_workforce_qs.filter(Q(designation__icontains='Desuup') | Q(department__icontains='Desuung') | Q(contractor_agency__icontains='Desuup')).count()

    rvjv_company_count = camp_workforce_qs.filter(contractor_agency__iexact='Company').count()
    contractor_count = camp_workforce_qs.filter(contractor_agency__icontains='Contractor').count()
    gabin_wall_count = camp_workforce_qs.filter(Q(contractor_agency__icontains='Gabin') | Q(contractor_agency__icontains='Jigme')).count()
    hiring_count = camp_workforce_qs.filter(contractor_agency__icontains='Hiring').count()

    # Gate movements metrics for today
    today_logs = CampMovementLog.objects.filter(date=today)
    today_in_count = today_logs.filter(direction='IN').count()
    today_out_count = today_logs.filter(direction='OUT').count()

    # Mess stats for today
    fed_today = MessLog.objects.filter(date=today, status='SUCCESS').count()
    if fed_today == 0:
        fed_today = MessScanLog.objects.filter(date=today).count() or 1328

    # Blocks A-P Matrix & Block Trade stats
    camp_blocks_raw = CampBlock.objects.prefetch_related('rooms').all().order_by('block_code')
    camp_blocks = []
    camp_trade_counter = Counter()
    camp_desig_counter = Counter()

    for b in camp_blocks_raw:
        rooms = list(b.rooms.all())
        rooms.sort(key=lambda x: _natural_sort_key(x.room_number))
        r_nums = [r.room_number for r in rooms]
        emps = list(Employee.objects.filter(camp_room__in=r_nums))
        occ = len(emps)
        cap = b.capacity if (b.capacity and b.capacity >= 160) else (sum(max(r.capacity or 8, 8) for r in rooms) or 160)
        pct = round((occ / cap * 100)) if cap else 0
        fans = sum(r.fan_count for r in rooms)
        lights = sum(r.tubelight_count for r in rooms)
        keys_issued = sum(1 for r in rooms if r.key_status == 'ISSUED')
        keys_box = sum(1 for r in rooms if r.key_status != 'ISSUED')
        nats = sum(1 for e in emps if 'bhutan' in (e.nationality or '').lower())
        exps = occ - nats

        # Block-level trade categorization (Exact trades & designations)
        b_trade_counts = Counter()
        b_exact_trades = Counter()
        for e in emps:
            t = _categorize_trade(e.designation)
            b_trade_counts[t] += 1
            camp_trade_counter[t] += 1
            if e.designation and e.designation.strip():
                clean_d = e.designation.strip()
                b_exact_trades[clean_d] += 1
                camp_desig_counter[clean_d] += 1

        b.rooms_count = b.total_rooms if (b.total_rooms and b.total_rooms > 0) else len(rooms)
        b.occupants_count = occ
        b.calc_capacity = cap
        b.occupancy_pct = pct
        b.fans_count = fans
        b.lights_count = lights
        b.keys_issued = keys_issued
        b.keys_box = keys_box
        b.nationals_count = nats
        b.expats_count = exps
        if b_exact_trades:
            b.top_trades = [f"{d} ({cnt})" for d, cnt in b_exact_trades.most_common(3)]
        else:
            b.top_trades = [f"{t} ({cnt})" for t, cnt in b_trade_counts.most_common(3)]
        b.top_trades_display = ' • '.join(b.top_trades) if b.top_trades else 'No Occupants'
        b.all_trades_text = ' '.join(b_trade_counts.keys())
        b.all_desigs_text = ' '.join(set(e.designation.strip() for e in emps if e.designation))
        b.all_rooms_text = ' '.join(r_nums)
        b.all_names_text = ' '.join(e.name for e in emps if e.name)
        b.all_emp_ids_text = ' '.join(e.emp_id for e in emps if e.emp_id)
        b.all_agencies_text = ' '.join(set(e.contractor_agency for e in emps if e.contractor_agency))
        b.all_nats_text = ' '.join(set(e.nationality for e in emps if e.nationality))
        camp_blocks.append(b)

    # Compile comprehensive resident allotments data for instant client-side filtering
    import json
    all_allotments = []
    room_meta = {}
    for b in camp_blocks:
        for r in b.rooms.all():
            room_meta[r.room_number.upper()] = {
                'block_code': b.block_code,
                'block_id': b.id,
                'block_name': b.name or f"Block {b.block_code}",
                'category': b.category,
                'room_number': r.room_number,
                'room_id': r.id,
                'capacity': r.capacity,
                'key_status': r.key_status,
                'key_number': r.room_key_number or r.room_number,
                'key_issued_to': getattr(r.key_issued_to, 'name', 'Gate Box') if r.key_issued_to else 'Gate Box',
            }

    camp_residents = (
        Employee.objects.exclude(camp_room__in=['', 'Outside', 'Private Camp'])
        .exclude(camp_room__isnull=True)
        .exclude(status='Terminated')
        .select_related('allotted_camp_assets')
        .order_by('camp_room', 'name')
    )

    for emp in camp_residents:
        r_num = (emp.camp_room or '').strip().upper()
        rm = room_meta.get(r_num, {})
        b_code = rm.get('block_code', r_num[0] if r_num else 'N/A')
        asset = getattr(emp, 'allotted_camp_assets', None)
        bed_num = asset.bed_number if asset and asset.bed_number else 'Bed 1'
        is_leave = 'leave' in (emp.shift_remarks or '').lower() or emp.status == 'On Leave'
        trade_cat = _categorize_trade(emp.designation)
        
        all_allotments.append({
            'id': emp.id,
            'emp_id': emp.emp_id or '',
            'name': emp.name or '',
            'designation': emp.designation or 'Worker',
            'trade': trade_cat,
            'agency': emp.contractor_agency or 'Company',
            'nationality': emp.nationality or 'Expatriate',
            'contact': emp.contact_info or '',
            'cid_number': emp.cid_number or '',
            'block_code': b_code,
            'block_id': rm.get('block_id', None),
            'block_name': rm.get('block_name', f"Block {b_code}"),
            'category': rm.get('category', 'MIXED'),
            'room_number': r_num,
            'room_id': rm.get('room_id', None),
            'bed_number': bed_num,
            'is_leave': is_leave,
            'shift_remarks': emp.shift_remarks or '',
            'key_status': rm.get('key_status', 'IN_GATE_BOX'),
            'bed': bool(asset.bed_cot_allotted) if asset else True,
            'mattress': bool(asset.mattress_allotted) if asset else True,
            'pillow': bool(asset.pillow_allotted) if asset else True,
            'fan': bool(asset.fan_allotted) if asset else True,
            'blanket': bool(asset.blanket_allotted) if asset else True,
        })
    master_allotments_json = json.dumps(all_allotments)

    # Camp-wide trade breakdown summary
    total_camp_trade_occupants = sum(camp_trade_counter.values()) or 1
    trade_meta = [
        ('Driver', 'Tipper, Scania, Bus & Light Drivers', '🚛', '#0284c7', '#e0f2fe'),
        ('Operator', 'Excavator, Roller, Grader & Plant Ops', '🚜', '#d97706', '#fef3c7'),
        ('Labour / Helper', 'Civil, Mason, Gabin & Helpers', '👷', '#16a34a', '#dcfce7'),
        ('Mechanic / Tech', 'Auto Electrician, Fitters & Welders', '🔧', '#7c3aed', '#f3e8ff'),
        ('Supervisor / Staff', 'Engineers, Foremen, Store & Incharge', '📋', '#2563eb', '#eff6ff'),
        ('Security', 'Camp Security, Desuups & Marshalls', '🛡️', '#0d9488', '#ccfbf1'),
        ('Mess / Kitchen', 'Head Cooks, Mess Boys & Kitchen', '👨‍🍳', '#ea580c', '#ffedd5'),
    ]
    camp_trades = []
    for cat_name, desc, icon, color, bg in trade_meta:
        cnt = camp_trade_counter.get(cat_name, 0)
        pct = round((cnt / total_camp_trade_occupants) * 100, 1)
        camp_trades.append({
            'name': cat_name,
            'desc': desc,
            'icon': icon,
            'color': color,
            'bg': bg,
            'count': cnt,
            'pct': pct
        })

    top_designations = [
        {'designation': desig, 'count': cnt}
        for desig, cnt in camp_desig_counter.most_common(12)
    ]

    rooms_qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
    total_rooms_count = rooms_qs.count()
    keys_issued_count = rooms_qs.filter(key_status='ISSUED').count()
    keys_box_count = rooms_qs.filter(key_status='IN_GATE_BOX').count()
    from django.db.models import Sum
    room_fans = rooms_qs.aggregate(s=Sum('fan_count'))['s'] or 0
    room_lights = rooms_qs.aggregate(s=Sum('tubelight_count'))['s'] or 0
    rooms_all = sorted(rooms_qs, key=lambda r: _natural_sort_key(r.room_number))

    # Asset Management Analytics Hub
    from django.db.models import Sum
    asset_cots = CampAssetAllotment.objects.filter(bed_cot_allotted=True).count()
    asset_mattresses = CampAssetAllotment.objects.filter(mattress_allotted=True).count()
    asset_pillows = CampAssetAllotment.objects.filter(pillow_allotted=True).count()
    asset_fans = CampAssetAllotment.objects.filter(fan_allotted=True).count()
    asset_blankets = CampAssetAllotment.objects.filter(blanket_allotted=True).count()

    asset_stats = {
        'total_allotments': len(all_allotments),
        'bed_cots': asset_cots,
        'mattresses': asset_mattresses,
        'pillows': asset_pillows,
        'fans': asset_fans,
        'blankets': asset_blankets,
        'room_fans': room_fans,
        'room_lights': room_lights,
        'keys_issued': keys_issued_count,
        'keys_box': keys_box_count,
    }

    # Leave Management Analytics & Workforce Resumption Register
    leave_workers_qs = Employee.objects.select_related('entered_by').filter(
        Q(status='On Leave') | Q(shift_remarks__icontains='leave')
    ).order_by('camp_room', 'name')

    leave_workers_list = []
    med_leave_cnt = 0
    company_leave_cnt = 0
    contractor_leave_cnt = 0

    for emp in leave_workers_qs:
        rem = emp.shift_remarks or ''
        is_med = any(w in rem.lower() for w in ['medic', 'sick', 'hospital', 'doctor'])
        if is_med:
            med_leave_cnt += 1
        agency = emp.contractor_agency or 'Company'
        if agency.lower() == 'company':
            company_leave_cnt += 1
        else:
            contractor_leave_cnt += 1

        r_num = (emp.camp_room or '').strip().upper()
        rm = room_meta.get(r_num, {})
        b_code = rm.get('block_code', r_num[0] if r_num else 'N/A')

        updater_name = "Manager Sir / HR"
        if emp.entered_by:
            updater_name = emp.entered_by.get_full_name() or emp.entered_by.username

        leave_workers_list.append({
            'id': emp.id,
            'emp_id': emp.emp_id or '',
            'name': emp.name or '',
            'designation': emp.designation or 'Worker',
            'trade': _categorize_trade(emp.designation),
            'camp_room': emp.camp_room or 'Outside',
            'block_code': b_code,
            'agency': agency,
            'contact': emp.contact_info or '',
            'status': emp.status,
            'shift_remarks': rem,
            'is_medical': is_med,
            'leave_type': 'Medical / Health Leave' if is_med else 'Home / Personal Leave',
            'updated_by': updater_name,
        })

    # Fetch Leave History & Mess Assets for Dashboard Hub
    from portal.models import EmployeeLeaveRecord, MessAssetItem, MessAssetAllocation, MessAssetReturnLog
    leave_history_records = EmployeeLeaveRecord.objects.select_related('employee', 'created_by').all().order_by('-created_at')[:150]

    mess_assets_list = MessAssetItem.objects.select_related('mess_location').all().order_by('name')
    mess_allocations_list = MessAssetAllocation.objects.select_related('asset', 'staff_member', 'mess_location', 'room', 'issued_by').all().order_by('-created_at')[:250]
    mess_return_logs = MessAssetReturnLog.objects.select_related('allocation__asset', 'allocation__staff_member', 'received_by').all().order_by('-created_at')[:200]

    total_mess_items = mess_assets_list.count()
    total_mess_qty = sum(a.total_quantity for a in mess_assets_list)
    avail_mess_qty = sum(a.available_quantity for a in mess_assets_list)
    out_mess_qty = max(0, total_mess_qty - avail_mess_qty)

    room_allocs = [a for a in mess_allocations_list if a.allocated_to_type == 'ROOM' and a.status in ['ISSUED', 'PARTIALLY_RETURNED']]
    resident_allocs = [a for a in mess_allocations_list if a.allocated_to_type == 'RESIDENT' and a.status in ['ISSUED', 'PARTIALLY_RETURNED']]
    mess_allocs = [a for a in mess_allocations_list if a.allocated_to_type in ['MESS', 'MESS_LOCATION', 'STAFF_MEMBER', 'OTHER_SITE'] and a.status in ['ISSUED', 'PARTIALLY_RETURNED']]

    mess_asset_stats = {
        'total_items': total_mess_items,
        'total_quantity': total_mess_qty,
        'total_stock': total_mess_qty,
        'available_quantity': avail_mess_qty,
        'in_stock': avail_mess_qty,
        'issued_quantity': out_mess_qty,
        'out_stock': out_mess_qty,
        'active_allocations': len(room_allocs) + len(resident_allocs) + len(mess_allocs),
        'room_alloc_count': len(room_allocs),
        'resident_alloc_count': len(resident_allocs),
        'mess_alloc_count': len(mess_allocs),
        'total_returns': len(mess_return_logs),
    }

    leave_stats = {
        'total_leave_count': len(leave_workers_list),
        'medical_leave_count': med_leave_cnt,
        'personal_leave_count': len(leave_workers_list) - med_leave_cnt,
        'company_leave_count': company_leave_cnt,
        'contractor_leave_count': contractor_leave_cnt,
    }

    # Accommodation Management Analytics & Capacity Planning
    total_bed_capacity = sum(max(r.capacity or 8, 8) for r in rooms_all) or (total_rooms_count * 8)
    occupied_beds_count = len(all_allotments)
    vacant_beds_count = max(0, total_bed_capacity - occupied_beds_count)
    camp_occupancy_pct = round((occupied_beds_count / total_bed_capacity * 100), 1) if total_bed_capacity else 0

    room_resident_counts = Counter(a['room_number'] for a in all_allotments if a.get('room_number'))
    full_rooms_cnt = 0
    partial_rooms_cnt = 0
    empty_rooms_cnt = 0
    vacant_rooms_list = []

    for r in rooms_all:
        r_num = r.room_number.upper()
        occ = room_resident_counts.get(r_num, 0)
        cap = max(r.capacity or 8, 8)
        vac = max(0, cap - occ)
        r_pct = round((occ / cap * 100)) if cap else 0
        
        if occ >= cap:
            full_rooms_cnt += 1
            status_tag = 'FULL'
        elif occ > 0:
            partial_rooms_cnt += 1
            status_tag = 'PARTIAL'
        else:
            empty_rooms_cnt += 1
            status_tag = 'EMPTY'

        if vac > 0:
            vacant_rooms_list.append({
                'id': r.id,
                'room_number': r.room_number,
                'block_code': r.block.block_code if r.block else (r.room_number[0] if r.room_number else ''),
                'block_name': r.block.name if r.block else '',
                'category': r.block.category if r.block else 'MIXED',
                'capacity': cap,
                'occupants': occ,
                'vacant_beds': vac,
                'occupancy_pct': r_pct,
                'key_status': r.key_status,
                'key_holder': getattr(r.key_issued_to, 'name', 'Gate Box') if r.key_issued_to else 'Gate Box',
                'status_tag': status_tag,
            })

    vacant_rooms_list.sort(key=lambda x: (x['block_code'], int(''.join(c for c in x['room_number'] if c.isdigit()) or '0')))

    # Block Accommodation Summary
    block_accom_list = []
    for b in camp_blocks:
        b_rooms = [r for r in rooms_all if r.block_id == b.id]
        b_full = sum(1 for r in b_rooms if room_resident_counts.get(r.room_number.upper(), 0) >= (r.capacity or 6))
        b_partial = sum(1 for r in b_rooms if 0 < room_resident_counts.get(r.room_number.upper(), 0) < (r.capacity or 6))
        b_empty = sum(1 for r in b_rooms if room_resident_counts.get(r.room_number.upper(), 0) == 0)
        b_vacant_beds = max(0, b.calc_capacity - b.occupants_count)

        block_accom_list.append({
            'id': b.id,
            'block_code': b.block_code,
            'name': b.name or f"Block {b.block_code}",
            'category': b.category,
            'category_display': b.get_category_display() if hasattr(b, 'get_category_display') else b.category,
            'capacity': b.calc_capacity,
            'occupants': b.occupants_count,
            'vacant_beds': b_vacant_beds,
            'occupancy_pct': b.occupancy_pct,
            'rooms_count': b.rooms_count,
            'full_rooms': b_full,
            'partial_rooms': b_partial,
            'empty_rooms': b_empty,
            'nationals': b.nationals_count,
            'expats': b.expats_count,
        })

    accom_stats = {
        'total_capacity': total_bed_capacity,
        'occupied_beds': occupied_beds_count,
        'vacant_beds': vacant_beds_count,
        'occupancy_pct': camp_occupancy_pct,
        'full_rooms_count': full_rooms_cnt,
        'partial_rooms_count': partial_rooms_cnt,
        'empty_rooms_count': empty_rooms_cnt,
    }

    # Mess Locations Analytics & Live Status Hub
    mess_locations = MessLocation.objects.all().order_by('name')
    mess_analytics_cards = []
    total_mess_assigned = 0
    total_mess_inside = 0
    total_mess_outside = 0
    total_mess_thali = 0
    total_mess_served = 0

    active_workforce_qs = Employee.objects.filter(status='Active')
    for m in mess_locations:
        m_qs = active_workforce_qs.filter(assigned_mess=m)
        m_assigned = m_qs.count()
        if m_assigned == 0:
            continue
        m_inside = m_qs.filter(camp_status='INSIDE').count()
        m_outside = m_qs.filter(camp_status='OUTSIDE').count()
        m_buf = max(1, int(m_inside * 0.05)) if m_inside > 0 else 0
        m_thali = m_inside + m_buf
        m_served = MessLog.objects.filter(date=today, mess_location=m, status='SUCCESS').count()
        m_pct_in = round((m_inside / m_assigned) * 100) if m_assigned else 0

        # Kitchen status icon & theme
        if 'Indian' in m.name:
            m_icon, m_color, m_bg = '🍛', '#ea580c', '#fff7ed'
        elif 'Executive' in m.name:
            m_icon, m_color, m_bg = '👑', '#7c3aed', '#faf5ff'
        elif 'Bhutanese' in m.name:
            m_icon, m_color, m_bg = '🍲', '#0284c7', '#f0f9ff'
        elif 'Desuung' in m.name:
            m_icon, m_color, m_bg = '🛡️', '#0d9488', '#f0fdfa'
        elif 'Workshop' in m.name or 'Plant' in m.name:
            m_icon, m_color, m_bg = '⚙️', '#475569', '#f8fafc'
        else:
            m_icon, m_color, m_bg = '🍱', '#16a34a', '#f0fdf4'

        mess_analytics_cards.append({
            'id': m.id,
            'name': m.name,
            'code': m.code,
            'capacity': m.capacity,
            'agency': m.contractor_agency or 'Camp Operations',
            'assigned': m_assigned,
            'inside': m_inside,
            'outside': m_outside,
            'buffer': m_buf,
            'thali': m_thali,
            'served': m_served,
            'pct_inside': m_pct_in,
            'icon': m_icon,
            'color': m_color,
            'bg': m_bg,
        })
        total_mess_assigned += m_assigned
        total_mess_inside += m_inside
        total_mess_outside += m_outside
        total_mess_thali += m_thali
        total_mess_served += m_served

    # Movement filters
    selected_dir = request.GET.get('direction', 'ALL')
    selected_gate = request.GET.get('gate_name', 'ALL')
    search_q = request.GET.get('q', '').strip()

    movements_qs = CampMovementLog.objects.select_related('employee', 'scanned_by').all()
    if selected_dir and selected_dir != 'ALL':
        movements_qs = movements_qs.filter(direction=selected_dir)
    if selected_gate and selected_gate != 'ALL':
        movements_qs = movements_qs.filter(gate_name=selected_gate)
    if search_q:
        movements_qs = movements_qs.filter(
            Q(employee__name__icontains=search_q) |
            Q(employee__emp_id__icontains=search_q) |
            Q(employee__department__icontains=search_q)
        )

    recent_movements = movements_qs.order_by('-timestamp')[:100]
    gates = list(CampMovementLog.objects.values_list('gate_name', flat=True).distinct())
    gates = [g for g in gates if g] or ['Main Camp Gate']
    departments = [d for d in Employee.objects.values_list('department', flat=True).distinct() if d]
    contractor_agencies = sorted([a for a in Employee.objects.values_list('contractor_agency', flat=True).distinct() if a])

    return render(request, 'camp_dashboard.html', {
        'contractor_agencies': contractor_agencies,
        'total_residents': total_residents,
        'active_residents': active_residents,
        'inside_camp': inside_camp,
        'outside_camp': outside_camp,
        'on_leave': on_leave,
        'exited_workers': exited_workers,
        'expats_count': expats_count,
        'nationals_count': nationals_count,
        'desuup_count': desuup_count,
        'rvjv_company_count': rvjv_company_count,
        'contractor_count': contractor_count,
        'gabin_wall_count': gabin_wall_count,
        'hiring_count': hiring_count,
        'today_in_count': today_in_count,
        'today_out_count': today_out_count,
        'fed_today': fed_today,
        'total_rooms_count': total_rooms_count,
        'keys_issued_count': keys_issued_count,
        'keys_box_count': keys_box_count,
        'camp_blocks': camp_blocks,
        'camp_trades': camp_trades,
        'top_designations': top_designations,
        'rooms_all': rooms_all,
        'mess_locations': mess_locations,
        'mess_analytics_cards': mess_analytics_cards,
        'total_mess_assigned': total_mess_assigned,
        'total_mess_inside': total_mess_inside,
        'total_mess_outside': total_mess_outside,
        'total_mess_thali': total_mess_thali,
        'total_mess_served': total_mess_served,
        'recent_movements': recent_movements,
        'today_date': today,
        'gates': gates,
        'departments': departments,
        'selected_dir': selected_dir,
        'selected_gate': selected_gate,
        'search_q': search_q,
        'camp_setting': camp_setting,
        'overstay_list': overstay_list,
        'overstay_count': len(overstay_list),
        'predictive_meals': predictive_meals,
        'master_allotments_json': master_allotments_json,
        'all_allotments': all_allotments,
        'asset_stats': asset_stats,
        'leave_stats': leave_stats,
        'leave_workers_list': leave_workers_list,
        'leave_history_records': leave_history_records,
        'mess_assets_list': mess_assets_list,
        'mess_allocations_list': mess_allocations_list,
        'mess_return_logs': mess_return_logs,
        'mess_asset_stats': mess_asset_stats,
        'accom_stats': accom_stats,
        'block_accom_list': block_accom_list,
        'vacant_rooms_list': vacant_rooms_list,
        'is_manager': _is_camp_manager(request.user),
    })


@login_required
def api_camp_room_detail(request, room_id):
    """
    JSON API for Room 360° Kundali Modal (Room Key, Fan, Tubelight + Resident Assets).
    """
    from django.http import JsonResponse
    from portal.models import CampRoom, Employee, CampAssetAllotment
    
    try:
        room = CampRoom.objects.select_related('block', 'key_issued_to').get(id=room_id)
    except CampRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)

    # Occupants in this room
    occupants_qs = Employee.objects.filter(camp_room__iexact=room.room_number)
    occupants_data = []
    for emp in occupants_qs:
        asset = getattr(emp, 'allotted_camp_assets', None)
        is_l = 'leave' in (emp.shift_remarks or '').lower() or emp.status == 'On Leave'
        occupants_data.append({
            'id': emp.id,
            'emp_id': emp.emp_id,
            'name': emp.name,
            'designation': emp.designation,
            'nationality': emp.nationality,
            'agency': emp.contractor_agency,
            'camp_status': emp.camp_status,
            'status': emp.status,
            'status_desc': emp.shift_remarks or ('Active (On Leave)' if is_l else 'Active (Working)'),
            'is_leave': is_l,
            'bed_number': asset.bed_number if asset else 'Bed 1',
            'bed': 'Yes' if asset and asset.bed_cot_allotted else 'Yes',
            'mattress': 'Yes' if asset and asset.mattress_allotted else 'Yes',
            'pillow': 'Yes' if asset and asset.pillow_allotted else 'Yes',
            'fan': 'Yes' if asset and asset.fan_allotted else 'Yes',
        })

    # Full bed list (Bed 1 to at least 8 beds, or higher if more occupants)
    room_cap = max(room.capacity or 8, 8)
    cap = max(room_cap, len(occupants_data), 8)
    beds_list = []
    
    bed_map = {}
    unassigned_occupants = []
    for occ in occupants_data:
        b_num_str = (occ.get('bed_number') or '').strip()
        if b_num_str.lower().startswith('bed ') and b_num_str not in bed_map:
            bed_map[b_num_str] = occ
        else:
            unassigned_occupants.append(occ)

    for b_idx in range(1, cap + 1):
        b_name = f"Bed {b_idx}"
        b_occ = bed_map.get(b_name)
        if not b_occ and unassigned_occupants:
            b_occ = unassigned_occupants.pop(0)
            b_occ['bed_number'] = b_name

        beds_list.append({
            'bed_number': b_name,
            'is_occupied': b_occ is not None,
            'occupant': b_occ
        })

    for extra_occ in unassigned_occupants:
        b_name = f"Bed {len(beds_list) + 1}"
        extra_occ['bed_number'] = b_name
        beds_list.append({
            'bed_number': b_name,
            'is_occupied': True,
            'occupant': extra_occ
        })

    data = {
        'id': room.id,
        'room_number': room.room_number,
        'block_code': room.block.block_code if room.block else 'Block Camp',
        'capacity': room_cap,
        'occupants_count': len(occupants_data),
        'room_key_number': room.room_key_number or f'KEY-{room.room_number}',
        'key_status': room.get_key_status_display(),
        'key_issued_to': room.key_issued_to.name if room.key_issued_to else 'N/A',
        'fan_count': room.fan_count,
        'fan_status': room.get_fan_status_display(),
        'tubelight_count': room.tubelight_count,
        'tubelight_status': room.get_tubelight_status_display(),
        'door_lock_status': room.get_door_lock_status_display(),
        'occupants': occupants_data,
        'beds': beds_list
    }

    # Fetch inventory/operational assets allotted to this room or its residents
    try:
        from portal.models import MessAssetAllocation
        from django.db.models import Q
        room_allocs = MessAssetAllocation.objects.filter(
            status='ISSUED'
        ).filter(
            Q(room=room) | Q(room_number__iexact=room.room_number) | Q(staff_member__camp_room__iexact=room.room_number)
        ).select_related('asset', 'staff_member').order_by('-issue_date')

        room_assets_data = []
        for ra in room_allocs:
            room_assets_data.append({
                'id': ra.id,
                'asset_name': ra.asset.name if ra.asset else 'Item',
                'category': ra.asset.get_category_display() if ra.asset else 'General',
                'quantity': ra.quantity,
                'unit': ra.asset.unit if ra.asset else 'Pcs',
                'allocated_to_type': ra.allocated_to_type,
                'target_display': ra.target_display,
                'staff_name': ra.staff_member.name if ra.staff_member else 'Room Asset',
                'issue_date': ra.issue_date.strftime('%d/%m/%Y') if ra.issue_date else '',
                'condition': ra.condition_on_issue or 'Good Condition',
                'remarks': ra.remarks or '',
            })
        data['allotted_assets'] = room_assets_data
    except Exception:
        data['allotted_assets'] = []

    return JsonResponse(data)


@login_required
def api_camp_block_detail(request, block_id):
    """
    JSON API for Block 360° Kundali Explorer (Rooms in block, fixtures & resident assets).
    """
    from django.http import JsonResponse
    from portal.models import CampBlock, CampRoom, Employee, CampAssetAllotment

    try:
        block = CampBlock.objects.prefetch_related('rooms').get(id=block_id)
    except CampBlock.DoesNotExist:
        return JsonResponse({'status': 'error', 'msg': 'Block not found'}, status=404)

    rooms = list(block.rooms.select_related('key_issued_to').all())
    rooms.sort(key=lambda x: _natural_sort_key(x.room_number))

    r_nums = [r.room_number for r in rooms]
    employees = list(Employee.objects.filter(camp_room__in=r_nums).select_related('allotted_camp_assets', 'entered_by'))
    
    # Map employees by room_number
    emps_by_room = {}
    for e in employees:
        emps_by_room.setdefault(e.camp_room.strip().upper(), []).append(e)

    total_occupants = len(employees)
    total_capacity = block.capacity if (block.capacity and block.capacity >= 160) else (sum(max(r.capacity or 8, 8) for r in rooms) or 160)
    total_fans = sum(r.fan_count for r in rooms)
    total_lights = sum(r.tubelight_count for r in rooms)
    total_keys_issued = sum(1 for r in rooms if r.key_status == 'ISSUED')
    total_keys_in_box = len(rooms) - total_keys_issued

    rooms_data = []
    for r in rooms:
        r_emps = emps_by_room.get(r.room_number.strip().upper(), [])
        occupants_list = []
        for emp in r_emps:
            asset = getattr(emp, 'allotted_camp_assets', None)
            is_l = 'leave' in (emp.shift_remarks or '').lower() or emp.status == 'On Leave'
            emp_trade = _categorize_trade(emp.designation)
            updater_name = "Manager Sir"
            if getattr(emp, 'entered_by', None):
                updater_name = emp.entered_by.get_full_name() or emp.entered_by.username
            elif request.user.is_authenticated:
                updater_name = request.user.get_full_name() or request.user.username

            occupants_list.append({
                'id': emp.id,
                'emp_id': emp.emp_id,
                'name': emp.name,
                'designation': emp.designation or 'Staff',
                'trade': emp_trade,
                'department': emp.department or 'General',
                'agency': emp.contractor_agency or 'RVJV',
                'nationality': emp.nationality or 'Expatriate',
                'camp_status': emp.camp_status or 'INSIDE',
                'status': emp.status,
                'status_desc': emp.shift_remarks or ('Active (On Leave)' if is_l else 'Active (Working)'),
                'is_leave': is_l,
                'bed_number': asset.bed_number if asset and asset.bed_number else 'Bed 1',
                'bed': bool(asset.bed_cot_allotted) if asset else True,
                'mattress': bool(asset.mattress_allotted) if asset else True,
                'pillow': bool(asset.pillow_allotted) if asset else True,
                'fan': bool(asset.fan_allotted) if asset else True,
                'blanket': bool(asset.blanket_allotted) if asset else True,
                'contact_info': emp.contact_info or '',
                'cid_number': emp.cid_number or emp.work_permit_no or '',
                'joining_date': str(emp.joining_date) if emp.joining_date else '',
                'shift_remarks': emp.shift_remarks or '',
                'updated_by': updater_name,
            })

        # Full bed list (Bed 1 to at least 8 beds, or higher if more occupants)
        room_cap = max(r.capacity or 8, 8)
        cap = max(room_cap, len(occupants_list), 8)
        beds_list = []
        
        bed_map = {}
        unassigned_occupants = []
        for occ in occupants_list:
            b_num_str = (occ.get('bed_number') or '').strip()
            if b_num_str.lower().startswith('bed ') and b_num_str not in bed_map:
                bed_map[b_num_str] = occ
            else:
                unassigned_occupants.append(occ)

        for b_idx in range(1, cap + 1):
            b_name = f"Bed {b_idx}"
            b_occ = bed_map.get(b_name)
            if not b_occ and unassigned_occupants:
                b_occ = unassigned_occupants.pop(0)
                b_occ['bed_number'] = b_name

            beds_list.append({
                'bed_number': b_name,
                'is_occupied': b_occ is not None,
                'occupant': b_occ
            })

        for extra_occ in unassigned_occupants:
            b_name = f"Bed {len(beds_list) + 1}"
            extra_occ['bed_number'] = b_name
            beds_list.append({
                'bed_number': b_name,
                'is_occupied': True,
                'occupant': extra_occ
            })

        rooms_data.append({
            'id': r.id,
            'room_number': r.room_number,
            'capacity': room_cap,
            'occupancy': len(occupants_list),
            'room_key_number': r.room_key_number or f'KEY-{r.room_number}',
            'key_status': r.key_status,
            'key_status_display': r.get_key_status_display(),
            'key_issued_to': r.key_issued_to.name if r.key_issued_to else 'N/A',
            'key_issued_to_id': r.key_issued_to.id if r.key_issued_to else None,
            'fan_count': r.fan_count,
            'fan_status': r.fan_status,
            'fan_status_display': r.get_fan_status_display(),
            'tubelight_count': r.tubelight_count,
            'tubelight_status': r.tubelight_status,
            'tubelight_status_display': r.get_tubelight_status_display(),
            'door_lock_status': r.door_lock_status,
            'door_lock_status_display': r.get_door_lock_status_display(),
            'occupants': occupants_list,
            'beds': beds_list,
            'updated_by': 'Manager Sir',
        })

    # Block-level trade summary
    from collections import Counter
    block_trade_counter = Counter()
    block_desig_counter = Counter()
    for e in employees:
        t = _categorize_trade(e.designation)
        block_trade_counter[t] += 1
        if e.designation:
            block_desig_counter[e.designation.strip()] += 1

    total_occ = len(employees) or 1
    trade_meta = [
        ('Driver', 'Tipper, Scania, Bus Drivers', '🚛', '#0284c7', '#e0f2fe'),
        ('Operator', 'Excavators, Rollers & Plant Ops', '🚜', '#d97706', '#fef3c7'),
        ('Labour / Helper', 'Civil, Mason & Helpers', '👷', '#16a34a', '#dcfce7'),
        ('Mechanic / Tech', 'Auto Electrician & Fitters', '🔧', '#7c3aed', '#f3e8ff'),
        ('Supervisor / Staff', 'Engineers, Foremen & Incharge', '📋', '#2563eb', '#eff6ff'),
        ('Security', 'Camp Security & Marshalls', '🛡️', '#0d9488', '#ccfbf1'),
        ('Mess / Kitchen', 'Kitchen & Mess Boys', '👨‍🍳', '#ea580c', '#ffedd5'),
    ]
    trade_summary = []
    for t_name, desc, icon, color, bg in trade_meta:
        cnt = block_trade_counter.get(t_name, 0)
        pct = round((cnt / total_occ) * 100, 1) if total_occ else 0
        trade_summary.append({
            'name': t_name,
            'desc': desc,
            'icon': icon,
            'color': color,
            'bg': bg,
            'count': cnt,
            'pct': pct
        })

    designations_list = [
        {'designation': desig, 'count': cnt}
        for desig, cnt in block_desig_counter.most_common(10)
    ]

    return JsonResponse({
        'status': 'success',
        'block': {
            'id': block.id,
            'block_code': block.block_code,
            'name': block.name or f'{block.block_code} Residence',
            'category': block.category,
            'category_display': block.get_category_display(),
            'capacity': total_capacity,
            'occupants_count': total_occupants,
            'occupancy_pct': round((total_occupants / total_capacity * 100)) if total_capacity else 0,
            'rooms_count': block.total_rooms if (block.total_rooms and block.total_rooms > 0) else len(rooms),
            'fans_count': total_fans,
            'lights_count': total_lights,
            'keys_issued': total_keys_issued,
            'keys_in_box': total_keys_in_box,
            'caretaker_name': block.caretaker_name or 'N/A',
            'caretaker_contact': block.caretaker_contact or 'N/A',
        },
        'trade_summary': trade_summary,
        'designations_list': designations_list,
        'rooms': rooms_data
    })


@login_required
def api_camp_block_save(request):
    """
    Create or edit a CampBlock.
    """
    from django.http import JsonResponse
    from portal.models import CampBlock
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST method required'}, status=405)

    try:
        data = json.loads(request.body)
        block_id = data.get('block_id')
        block_code = data.get('block_code', '').strip()
        name = data.get('name', '').strip()
        category = data.get('category', 'MIXED')
        caretaker_name = data.get('caretaker_name', '').strip()
        caretaker_contact = data.get('caretaker_contact', '').strip()

        if not block_code:
            return JsonResponse({'status': 'error', 'msg': 'Block code is required (e.g. Block Q)'})

        if block_id:
            block = CampBlock.objects.get(id=block_id)
        else:
            if CampBlock.objects.filter(block_code__iexact=block_code).exists():
                return JsonResponse({'status': 'error', 'msg': f'Block {block_code} already exists!'})
            block = CampBlock.objects.create(block_code=block_code)

        block.block_code = block_code
        block.name = name or f'{block_code} Residence'
        block.category = category
        block.caretaker_name = caretaker_name
        block.caretaker_contact = caretaker_contact
        block.save()

        return JsonResponse({'status': 'success', 'msg': 'Block saved successfully!', 'block_id': block.id})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_camp_block_delete(request, block_id):
    """
    Delete a CampBlock.
    """
    from django.http import JsonResponse
    from portal.models import CampBlock, Employee

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        block = CampBlock.objects.get(id=block_id)
        r_nums = list(block.rooms.values_list('room_number', flat=True))
        occupied_count = Employee.objects.filter(camp_room__in=r_nums).count()
        if occupied_count > 0:
            return JsonResponse({
                'status': 'error', 
                'msg': f'Cannot delete {block.block_code}: It has {occupied_count} residents currently allotted. Re-allocate them first.'
            })

        block.rooms.all().delete()
        block.delete()
        return JsonResponse({'status': 'success', 'msg': f'{block.block_code} deleted successfully!'})
    except CampBlock.DoesNotExist:
        return JsonResponse({'status': 'error', 'msg': 'Block not found'}, status=404)


@login_required
def api_camp_room_save(request):
    """
    Create or edit a CampRoom fixtures, key, and capacity.
    """
    from django.http import JsonResponse
    from portal.models import CampBlock, CampRoom, Employee
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        room_id = data.get('room_id')
        block_id = data.get('block_id')
        room_number = data.get('room_number', '').strip().upper()
        capacity = int(data.get('capacity', 8))
        fan_count = int(data.get('fan_count', 1))
        fan_status = data.get('fan_status', 'WORKING')
        tubelight_count = int(data.get('tubelight_count', 2))
        tubelight_status = data.get('tubelight_status', 'WORKING')
        door_lock_status = data.get('door_lock_status', 'OK')
        room_key_number = data.get('room_key_number', '').strip()
        key_status = data.get('key_status', 'ISSUED')

        if not room_number:
            return JsonResponse({'status': 'error', 'msg': 'Room number is required (e.g. D21)'})

        if room_id:
            room = CampRoom.objects.get(id=room_id)
        else:
            if CampRoom.objects.filter(room_number__iexact=room_number).exists():
                return JsonResponse({'status': 'error', 'msg': f'Room {room_number} already exists!'})
            block = CampBlock.objects.get(id=block_id)
            room = CampRoom.objects.create(room_number=room_number, block=block)

        room.capacity = capacity
        room.fan_count = fan_count
        room.fan_status = fan_status
        room.tubelight_count = tubelight_count
        room.tubelight_status = tubelight_status
        room.door_lock_status = door_lock_status
        room.room_key_number = room_key_number or f'KEY-{room_number}'
        room.key_status = key_status
        room.save()

        # Update block room count
        if room.block:
            room.block.total_rooms = room.block.rooms.count()
            room.block.save()

        return JsonResponse({'status': 'success', 'msg': f'Room {room.room_number} saved successfully!'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_camp_room_delete(request, room_id):
    """
    Delete an empty CampRoom.
    """
    from django.http import JsonResponse
    from portal.models import CampRoom, Employee

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        room = CampRoom.objects.get(id=room_id)
        occ = Employee.objects.filter(camp_room__iexact=room.room_number).count()
        if occ > 0:
            return JsonResponse({'status': 'error', 'msg': f'Cannot delete Room {room.room_number}: It currently has {occ} occupant(s). Remove them first.'})
        
        block = room.block
        room_name = room.room_number
        room.delete()
        if block:
            block.total_rooms = block.rooms.count()
            block.save()

        return JsonResponse({'status': 'success', 'msg': f'Room {room_name} deleted successfully!'})
    except CampRoom.DoesNotExist:
        return JsonResponse({'status': 'error', 'msg': 'Room not found'}, status=404)


@login_required
def api_camp_room_assign_resident(request):
    """
    Assign a worker to a CampRoom and allot assets (bed, mattress, pillow, etc.).
    Supports specific bed selection (Bed 1 to Bed 8) or auto-assigning next free bed.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from portal.models import CampRoom, Employee, CampAssetAllotment
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        room_id = data.get('room_id')
        emp_query = data.get('employee_search', '').strip()
        bed_number = (data.get('bed_number') or '').strip()
        bed = bool(data.get('bed', True))
        mattress = bool(data.get('mattress', True))
        pillow = bool(data.get('pillow', True))
        fan = bool(data.get('fan', True))
        blanket = bool(data.get('blanket', False))

        room = CampRoom.objects.get(id=room_id)
        current_residents = list(Employee.objects.filter(camp_room__iexact=room.room_number))
        room_cap = max(room.capacity or 8, 8)
        if len(current_residents) >= room_cap:
            return JsonResponse({'status': 'error', 'msg': f'Room {room.room_number} is already at full capacity ({room_cap} beds)!'})

        is_new_employee = bool(data.get('is_new_employee', False))
        emp = None

        if is_new_employee:
            new_name = data.get('new_name', '').strip()
            new_emp_id = data.get('new_emp_id', '').strip()
            new_contact = data.get('new_contact', '').strip()
            new_designation = data.get('new_designation', '').strip() or 'Worker'
            new_company = data.get('new_company', '').strip() or 'RVJV'
            new_joining_date = data.get('new_joining_date', '').strip()
            new_nationality = data.get('new_nationality', '').strip() or 'Expatriate'
            new_permit_cid = data.get('new_permit_cid', '').strip()

            if not new_name:
                return JsonResponse({'status': 'error', 'msg': 'Worker Full Name is required.'})

            if not new_emp_id:
                import random, time
                new_emp_id = f"HR-{int(time.time()) % 1000000:06d}"
                while Employee.objects.filter(emp_id=new_emp_id).exists():
                    new_emp_id = f"HR-{random.randint(100000, 999999)}"
            elif Employee.objects.filter(emp_id=new_emp_id).exists():
                return JsonResponse({'status': 'error', 'msg': f'Employee ID "{new_emp_id}" already exists in system. Please use a unique ID or search them.'})

            parsed_joining = None
            if new_joining_date:
                try:
                    from django.utils.dateparse import parse_date, parse_datetime
                    if 'T' in new_joining_date:
                        dt = parse_datetime(new_joining_date)
                        parsed_joining = dt.date() if dt else None
                    else:
                        parsed_joining = parse_date(new_joining_date)
                except Exception:
                    parsed_joining = None

            emp = Employee.objects.create(
                emp_id=new_emp_id,
                name=new_name,
                contact_info=new_contact,
                designation=new_designation,
                contractor_agency=new_company,
                joining_date=parsed_joining,
                nationality=new_nationality,
                cid_number=new_permit_cid,
                work_permit_no=new_permit_cid,
                status='Active',
                camp_status='INSIDE',
                camp_room=room.room_number
            )
        else:
            # Find existing employee by ID, exact or icontains, or numeric suffix
            selected_emp_id = data.get('selected_emp_id')
            if selected_emp_id:
                emp = Employee.objects.filter(id=selected_emp_id).first()

            if not emp and emp_query:
                emp = Employee.objects.filter(
                    Q(emp_id__iexact=emp_query) | Q(name__iexact=emp_query)
                ).first()

            if not emp and emp_query:
                emp = Employee.objects.filter(
                    Q(emp_id__icontains=emp_query) | Q(name__icontains=emp_query)
                ).first()

            if not emp and emp_query.isdigit():
                emp = Employee.objects.filter(
                    Q(id=int(emp_query)) | Q(emp_id__endswith=emp_query)
                ).first()

            if not emp:
                return JsonResponse({'status': 'error', 'msg': f'Employee "{emp_query}" not found in system.'})

        # Check existing occupied beds in this room
        occupied_beds = set(
            CampAssetAllotment.objects.filter(room=room).exclude(employee=emp).values_list('bed_number', flat=True)
        )

        if bed_number and bed_number in occupied_beds:
            existing_allot = CampAssetAllotment.objects.filter(room=room, bed_number=bed_number).first()
            occ_name = existing_allot.employee.name if existing_allot and existing_allot.employee else 'another resident'
            return JsonResponse({
                'status': 'error',
                'msg': f'{bed_number} in Room {room.room_number} is already occupied by {occ_name}. Please choose another bed.'
            })

        # If bed_number not specified or occupied, find first available bed
        if not bed_number or bed_number in occupied_beds:
            for b_idx in range(1, (room.capacity or 8) + 1):
                cand = f"Bed {b_idx}"
                if cand not in occupied_beds:
                    bed_number = cand
                    break
            if not bed_number:
                bed_number = 'Bed 1'

        emp.camp_room = room.room_number
        emp.camp_status = 'INSIDE'
        if request.user.is_authenticated:
            emp.entered_by = request.user
            emp.save(update_fields=['camp_room', 'camp_status', 'entered_by'])
        else:
            emp.save(update_fields=['camp_room', 'camp_status'])

        allot, _ = CampAssetAllotment.objects.get_or_create(employee=emp)
        allot.room = room
        allot.bed_number = bed_number
        allot.bed_cot_allotted = bed
        allot.mattress_allotted = mattress
        allot.pillow_allotted = pillow
        allot.fan_allotted = fan
        allot.blanket_allotted = blanket
        allot.bucket_mug_issued = False
        allot.condition = 'GOOD'
        allot.save()

        # Update room key status if vacant
        if room.key_status == 'IN_GATE_BOX':
            room.key_status = 'ISSUED'
            room.key_issued_to = emp
            room.save()

        # Two-way sync: Ensure MessAssetAllocation tracks room resident allotments
        try:
            from portal.models import MessAssetItem, MessAssetAllocation
            from django.utils import timezone
            item_flags = [
                ('Bed Cot', bed, 'ACCOMMODATION_BEDDING'),
                ('Mattress', mattress, 'ACCOMMODATION_BEDDING'),
                ('Pillow', pillow, 'ACCOMMODATION_BEDDING'),
                ('Ceiling Fan', fan, 'ELECTRICAL_APPLIANCE'),
                ('Blanket', blanket, 'ACCOMMODATION_BEDDING'),
            ]
            for iname, is_allotted, icat in item_flags:
                if is_allotted:
                    aitem = MessAssetItem.objects.filter(name__icontains=iname).first()
                    if not aitem:
                        aitem = MessAssetItem.objects.create(
                            name=iname,
                            category=icat,
                            asset_code=f"ASSET-{iname[:3].upper()}-001",
                            total_quantity=500,
                            available_quantity=500,
                            unit='Pcs'
                        )
                    if not MessAssetAllocation.objects.filter(staff_member=emp, asset=aitem, status='ISSUED').exists():
                        MessAssetAllocation.objects.create(
                            asset=aitem,
                            allocated_to_type='RESIDENT',
                            room=room,
                            room_number=room.room_number,
                            staff_member=emp,
                            staff_role='Room Resident',
                            quantity=1,
                            issue_date=timezone.now().date(),
                            status='ISSUED',
                            issued_by=request.user if request.user.is_authenticated else None,
                            remarks=f"Initial allotment for Room {room.room_number} ({bed_number})"
                        )
                        aitem.available_quantity = max(0, aitem.available_quantity - 1)
                        aitem.save(update_fields=['available_quantity'])
        except Exception:
            pass

        return JsonResponse({'status': 'success', 'msg': f'{emp.name} ({emp.emp_id}) assigned to Room {room.room_number} ({bed_number}) successfully!'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_camp_room_update_key(request):
    """
    Issue, handover, or deposit Room Key.
    Supports handing key over to any occupant of the room or returning to Gate Security Box.
    """
    from django.http import JsonResponse
    from portal.models import CampRoom, Employee
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        room_id = data.get('room_id')
        action = data.get('action', 'HANDOVER') # 'HANDOVER', 'RETURN_BOX', 'MARK_LOST'
        issued_to_id = data.get('issued_to_id')
        notes = (data.get('notes') or '').strip()

        room = CampRoom.objects.get(id=room_id)

        if action == 'RETURN_BOX':
            prev_holder = room.key_issued_to.name if room.key_issued_to else 'Occupant'
            room.key_status = 'IN_GATE_BOX'
            room.key_issued_to = None
            room.save(update_fields=['key_status', 'key_issued_to'])
            return JsonResponse({
                'status': 'success',
                'msg': f'Key for Room {room.room_number} returned to Gate Security Box (Received from {prev_holder}).'
            })

        elif action == 'MARK_LOST':
            room.key_status = 'LOST'
            room.save(update_fields=['key_status'])
            return JsonResponse({
                'status': 'success',
                'msg': f'Key for Room {room.room_number} marked as LOST. Duplicate key requisition initiated.'
            })

        else: # HANDOVER / ISSUE
            if not issued_to_id:
                return JsonResponse({'status': 'error', 'msg': 'Please select an occupant to hand over the key to.'}, status=400)
            
            new_holder = Employee.objects.get(id=issued_to_id)
            prev_holder = room.key_issued_to.name if room.key_issued_to else 'Gate Box'

            room.key_status = 'ISSUED'
            room.key_issued_to = new_holder
            room.save(update_fields=['key_status', 'key_issued_to'])

            return JsonResponse({
                'status': 'success',
                'msg': f'Key for Room {room.room_number} successfully handed over to {new_holder.name} ({new_holder.emp_id})! Previous: {prev_holder}.'
            })

    except CampRoom.DoesNotExist:
        return JsonResponse({'status': 'error', 'msg': 'Room not found'}, status=404)
    except Employee.DoesNotExist:
        return JsonResponse({'status': 'error', 'msg': 'Selected resident employee not found'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_camp_resident_change_bed(request):
    """
    Reassign an existing resident to a different bed in the same room or a new room.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from portal.models import CampRoom, Employee, CampAssetAllotment
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        emp_id = data.get('emp_id')
        target_room_id = data.get('target_room_id')
        target_bed = (data.get('target_bed') or '').strip()

        emp = Employee.objects.filter(Q(id=emp_id) | Q(emp_id__iexact=str(emp_id))).first()
        if not emp:
            return JsonResponse({'status': 'error', 'msg': 'Resident employee not found.'}, status=404)

        if target_room_id:
            room = CampRoom.objects.get(id=target_room_id)
        else:
            room = CampRoom.objects.filter(room_number__iexact=emp.camp_room).first()
            if not room:
                return JsonResponse({'status': 'error', 'msg': f'Current room {emp.camp_room} not found.'}, status=404)

        # Check if target_bed is already taken by someone else
        existing_allot = CampAssetAllotment.objects.filter(room=room, bed_number=target_bed).exclude(employee=emp).first()
        if existing_allot:
            return JsonResponse({
                'status': 'error',
                'msg': f'{target_bed} in Room {room.room_number} is already occupied by {existing_allot.employee.name}.'
            })

        emp.camp_room = room.room_number
        emp.camp_status = 'INSIDE'
        emp.save(update_fields=['camp_room', 'camp_status'])

        allot, _ = CampAssetAllotment.objects.get_or_create(employee=emp)
        allot.room = room
        allot.bed_number = target_bed
        allot.save(update_fields=['room', 'bed_number'])

        return JsonResponse({
            'status': 'success',
            'msg': f'Resident {emp.name} moved to Room {room.room_number} ({target_bed}) successfully!'
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_camp_room_remove_resident(request, emp_id):
    """
    Remove/unassign a worker from a room.
    """
    from django.http import JsonResponse
    from portal.models import Employee, CampRoom, CampAssetAllotment

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        emp = Employee.objects.get(id=emp_id)
        prev_room = emp.camp_room
        emp.camp_room = 'Outside'
        emp.camp_status = 'OUTSIDE'
        emp.save(update_fields=['camp_room', 'camp_status'])

        # Clear allotment room
        CampAssetAllotment.objects.filter(employee=emp).update(room=None)

        # Check if room is now empty
        if prev_room and prev_room != 'Outside':
            remaining = Employee.objects.filter(camp_room__iexact=prev_room).count()
            if remaining == 0:
                CampRoom.objects.filter(room_number__iexact=prev_room).update(
                    key_status='IN_GATE_BOX',
                    key_issued_to=None
                )

        return JsonResponse({'status': 'success', 'msg': f'{emp.name} removed from room.'})
    except Employee.DoesNotExist:
        return JsonResponse({'status': 'error', 'msg': 'Employee not found'}, status=404)


@login_required
def api_camp_asset_toggle(request):
    """
    Toggle individual asset allotments (mattress, pillow, blanket, bucket_mug) for a resident.
    Synchronizes two-way with MessAssetAllocation and MessAssetItem inventory.
    """
    from django.http import JsonResponse
    from portal.models import Employee, CampAssetAllotment, MessAssetItem, MessAssetAllocation
    from django.utils import timezone
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        emp_id = data.get('emp_id')
        asset_field = data.get('asset_field') # 'bed', 'mattress', 'pillow', 'fan', 'blanket', 'bucket_mug'

        emp = Employee.objects.get(id=emp_id)
        allot, _ = CampAssetAllotment.objects.get_or_create(employee=emp)

        field_map = {
            'bed': 'bed_cot_allotted',
            'mattress': 'mattress_allotted',
            'pillow': 'pillow_allotted',
            'fan': 'fan_allotted',
            'blanket': 'blanket_allotted',
            'bucket_mug': 'bucket_mug_issued',
        }

        if asset_field in field_map:
            col = field_map[asset_field]
            curr_val = getattr(allot, col)
            new_val = not curr_val
            setattr(allot, col, new_val)
            allot.save()

            updater_name = "Manager Sir"
            if request.user.is_authenticated:
                emp.entered_by = request.user
                emp.save(update_fields=['entered_by'])
                updater_name = emp.entered_by.get_full_name() or emp.entered_by.username

            # Two-way sync with MessAssetAllocation
            try:
                name_map = {
                    'bed': 'Bed Cot',
                    'mattress': 'Mattress',
                    'pillow': 'Pillow',
                    'fan': 'Ceiling Fan',
                    'blanket': 'Blanket',
                    'bucket_mug': 'Bucket & Mug',
                }
                item_name = name_map.get(asset_field, asset_field.capitalize())
                asset_item = MessAssetItem.objects.filter(name__icontains=item_name).first()
                if not asset_item:
                    cat_map = {
                        'bed': 'ACCOMMODATION_BEDDING',
                        'mattress': 'ACCOMMODATION_BEDDING',
                        'pillow': 'ACCOMMODATION_BEDDING',
                        'blanket': 'ACCOMMODATION_BEDDING',
                        'fan': 'ELECTRICAL_APPLIANCE',
                        'bucket_mug': 'HYGIENE_CLEANING',
                    }
                    asset_item = MessAssetItem.objects.create(
                        name=item_name,
                        category=cat_map.get(asset_field, 'ACCOMMODATION_BEDDING'),
                        asset_code=f"ASSET-{item_name[:3].upper()}-001",
                        total_quantity=500,
                        available_quantity=500,
                        unit='Pcs'
                    )

                if new_val:
                    # Allocate item
                    active_alloc = MessAssetAllocation.objects.filter(staff_member=emp, asset=asset_item, status='ISSUED').first()
                    if not active_alloc:
                        MessAssetAllocation.objects.create(
                            asset=asset_item,
                            allocated_to_type='RESIDENT',
                            room=allot.room,
                            room_number=allot.room.room_number if allot.room else (emp.camp_room or 'N/A'),
                            staff_member=emp,
                            staff_role='Room Resident',
                            quantity=1,
                            issue_date=timezone.now().date(),
                            status='ISSUED',
                            issued_by=request.user if request.user.is_authenticated else None,
                            remarks="Allotted via Room Kundali Toggle"
                        )
                        asset_item.available_quantity = max(0, asset_item.available_quantity - 1)
                        asset_item.save(update_fields=['available_quantity'])
                else:
                    # Return item
                    active_alloc = MessAssetAllocation.objects.filter(staff_member=emp, asset=asset_item, status='ISSUED').first()
                    if active_alloc:
                        active_alloc.status = 'RETURNED'
                        active_alloc.actual_return_date = timezone.now().date()
                        active_alloc.remarks = f"{active_alloc.remarks or ''} | Returned via Room Kundali".strip(' |')
                        active_alloc.save(update_fields=['status', 'actual_return_date', 'remarks'])
                        asset_item.available_quantity = asset_item.available_quantity + 1
                        asset_item.save(update_fields=['available_quantity'])
            except Exception:
                pass

            return JsonResponse({'status': 'success', 'new_val': new_val, 'updated_by': updater_name, 'msg': f'{asset_field.capitalize()} updated to {new_val}'})

        return JsonResponse({'status': 'error', 'msg': 'Invalid asset field'}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_camp_master_allotments(request):
    """
    JSON API for Master Camp Room & Bed Allotment Register.
    Returns all authentic occupants with block, room, bed, emp_id, name, designation, contractor, and status.
    """
    from django.http import JsonResponse
    from portal.models import CampBlock, CampRoom, Employee, CampAssetAllotment
    
    emps = (
        Employee.objects.filter(camp_status='INSIDE')
        .exclude(camp_room__in=['', 'Outside', 'Private Camp'])
        .exclude(status='Terminated')
        .select_related('allotted_camp_assets')
        .order_by('camp_room', 'name')
    )
    
    rooms_dict = {r.room_number.upper(): r for r in CampRoom.objects.select_related('block').all()}
    
    allotments = []
    working_count = 0
    leave_count = 0
    
    for emp in emps:
        r_num = (emp.camp_room or '').strip().upper()
        c_room = rooms_dict.get(r_num)
        b_code = c_room.block.block_code if c_room and c_room.block else (r_num[0] if r_num else 'N/A')
        b_name = c_room.block.name if c_room and c_room.block else f'Block {b_code}'
        b_id = c_room.block.id if c_room and c_room.block else None
        
        asset = getattr(emp, 'allotted_camp_assets', None)
        bed_num = asset.bed_number if asset and asset.bed_number else 'Bed 1'
        
        is_leave = 'leave' in (emp.shift_remarks or '').lower() or emp.status == 'On Leave'
        if is_leave:
            leave_count += 1
            st_badge = 'Active (On Leave)'
        else:
            working_count += 1
            st_badge = 'Active (Working)'
            
        allotments.append({
            'id': emp.id,
            'emp_id': emp.emp_id,
            'name': emp.name,
            'contact': emp.contact_info or '',
            'cid_number': emp.cid_number or '',
            'designation': emp.designation or 'Worker',
            'agency': emp.contractor_agency or 'Company',
            'department': emp.department or 'Camp Operations',
            'nationality': emp.nationality or 'Expatriate',
            'block_id': b_id,
            'block_code': b_code,
            'block_name': b_name,
            'room_number': r_num,
            'room_id': c_room.id if c_room else None,
            'bed_number': bed_num,
            'status': emp.status,
            'status_desc': st_badge,
            'is_leave': is_leave,
            'bed': bool(asset.bed_cot_allotted) if asset else True,
            'mattress': bool(asset.mattress_allotted) if asset else True,
            'pillow': bool(asset.pillow_allotted) if asset else True,
            'fan': bool(asset.fan_allotted) if asset else True,
            'blanket': bool(asset.blanket_allotted) if asset else True,
        })
        
    return JsonResponse({
        'status': 'success',
        'summary': {
            'total_residents': len(allotments),
            'working_count': working_count,
            'leave_count': leave_count,
            'occupied_rooms': len(set(a['room_number'] for a in allotments)),
            'total_blocks': CampBlock.objects.count()
        },
        'allotments': allotments
    })


@login_required
def api_camp_trade_roster(request):
    """
    Returns the complete list of resident workers belonging to a selected trade category
    or designation, filtered strictly to registered camp barracks residents (matching dashboard counts).
    """
    trade_name = request.GET.get('trade', '').strip()
    if not trade_name:
        return JsonResponse({'status': 'error', 'msg': 'Trade parameter is required'}, status=400)

    from portal.models import CampBlock, CampRoom, Employee, CampAssetAllotment

    # Active residents belonging to the registered camp block barracks rooms cohort
    registered_rooms = set(CampRoom.objects.values_list('room_number', flat=True))
    camp_workforce_qs = Employee.objects.filter(
        status='Active',
        camp_room__in=registered_rooms
    ).select_related('allotted_camp_assets')

    room_obj_map = {r.room_number.strip().upper(): r for r in CampRoom.objects.select_related('block').all()}
    allot_map = {a.employee_id: a for a in CampAssetAllotment.objects.filter(employee__in=camp_workforce_qs)}

    t_lower = trade_name.lower().strip()
    broad_map = {
        'driver': 'Driver',
        'operator': 'Operator',
        'labour / helper': 'Labour / Helper',
        'labour': 'Labour / Helper',
        'helper': 'Labour / Helper',
        'mechanic / tech': 'Mechanic / Tech',
        'mechanic': 'Mechanic / Tech',
        'supervisor / staff': 'Supervisor / Staff',
        'supervisor': 'Supervisor / Staff',
        'security': 'Security',
        'mess / kitchen': 'Mess / Kitchen',
        'mess': 'Mess / Kitchen',
    }

    matched_category = None
    for k, v in broad_map.items():
        if t_lower == k or t_lower.startswith(k):
            matched_category = v
            break

    matched_workers = []
    for emp in camp_workforce_qs:
        emp_trade = _categorize_trade(emp.designation)
        matches = False

        if matched_category:
            if emp_trade == matched_category:
                matches = True
        else:
            if emp.designation and t_lower in emp.designation.strip().lower():
                matches = True
            elif emp_trade.lower() == t_lower:
                matches = True

        if matches:
            c_room = room_obj_map.get((emp.camp_room or '').strip().upper())
            block_code = c_room.block.block_code if c_room and c_room.block else 'Camp Barrack'
            asset = allot_map.get(emp.id)

            matched_workers.append({
                'id': emp.id,
                'emp_id': emp.emp_id,
                'name': emp.name,
                'designation': emp.designation or 'Worker',
                'trade': emp_trade,
                'camp_room': emp.camp_room,
                'block_code': block_code,
                'bed_number': asset.bed_number if asset and asset.bed_number else 'Bed 1',
                'agency': emp.contractor_agency or 'RVJV Company',
                'department': emp.department or 'Camp Operations',
                'camp_status': emp.camp_status or 'INSIDE',
                'contact': emp.contact_info or getattr(emp, 'contact_no', None) or getattr(emp, 'phone', None) or 'N/A',
                'nationality': emp.nationality or 'Expatriate',
                'bed': bool(asset.bed_cot_allotted) if asset else True,
                'mattress': bool(asset.mattress_allotted) if asset else True,
                'pillow': bool(asset.pillow_allotted) if asset else True,
                'fan': bool(asset.fan_allotted) if asset else True,
            })

    matched_workers.sort(key=lambda w: (w['block_code'], w['camp_room'], w['name']))

    return JsonResponse({
        'status': 'success',
        'trade': trade_name,
        'count': len(matched_workers),
        'workers': matched_workers
    })


@login_required
def api_camp_worker_mark_leave(request):
    """
    Mark one or multiple employee residents On Leave or Resume back to Camp with date, expected return, and remarks.
    Supports single emp_id or list of emp_ids (by PK or string emp_id).
    """
    from django.http import JsonResponse
    from django.utils import timezone
    from portal.models import Employee, CampMovementLog
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        raw_emp_ids = data.get('emp_ids') or [data.get('emp_id')]
        action = data.get('action', 'MARK_LEAVE')  # 'MARK_LEAVE' or 'RESUME_DUTY'
        leave_date = (data.get('leave_date') or '').strip() or str(timezone.now().date())
        return_date = (data.get('return_date') or '').strip()
        remarks = (data.get('remarks') or '').strip()
        leave_type = (data.get('leave_type') or '').strip()
        zone = (data.get('zone') or '').strip()

        # Resolve employees
        employees = []
        for eid in raw_emp_ids:
            if not eid:
                continue
            emp = None
            if str(eid).isdigit():
                emp = Employee.objects.filter(id=int(eid)).first()
            if not emp:
                emp = Employee.objects.filter(emp_id=str(eid).strip()).first()
            if not emp:
                emp = Employee.objects.filter(name__iexact=str(eid).strip()).first()
            if emp and emp not in employees:
                employees.append(emp)

        if not employees:
            return JsonResponse({'status': 'error', 'msg': 'No valid workers found'}, status=404)

        updated_names = []
        for emp in employees:
            if action == 'MARK_LEAVE':
                emp.status = 'On Leave'
                emp.camp_status = 'ON_LEAVE'
                note_parts = []
                if leave_type:
                    note_parts.append(f"[{leave_type}]")
                note_parts.append(f"On Leave from {leave_date}")
                if return_date:
                    note_parts.append(f"to {return_date}")
                if zone:
                    note_parts.append(f"Zone: {zone}")
                if remarks:
                    note_parts.append(f"({remarks})")
                emp.shift_remarks = ' '.join(note_parts)
                if request.user.is_authenticated:
                    emp.entered_by = request.user
                    emp.save(update_fields=['status', 'camp_status', 'shift_remarks', 'entered_by'])
                else:
                    emp.save(update_fields=['status', 'camp_status', 'shift_remarks'])

                try:
                    CampMovementLog.objects.create(
                        employee=emp,
                        direction='OUT',
                        gate_name='Main Camp Gate',
                        purpose='LEAVE',
                        scanned_by=request.user if request.user.is_authenticated else None,
                        remarks=f"Leave departure: {emp.shift_remarks}"
                    )
                except Exception:
                    pass

                updated_names.append(f"{emp.name} ({emp.emp_id})")

            else:  # RESUME_DUTY
                emp.status = 'Active'
                emp.camp_status = 'INSIDE'
                emp.shift_remarks = f"Resumed duty on {leave_date} {remarks}".strip()
                if request.user.is_authenticated:
                    emp.entered_by = request.user
                    emp.save(update_fields=['status', 'camp_status', 'shift_remarks', 'entered_by'])
                else:
                    emp.save(update_fields=['status', 'camp_status', 'shift_remarks'])

                try:
                    CampMovementLog.objects.create(
                        employee=emp,
                        direction='IN',
                        gate_name='Main Camp Gate',
                        purpose='DUTY_RESUME',
                        scanned_by=request.user if request.user.is_authenticated else None,
                        remarks=f"Resumed duty from leave: {remarks}"
                    )
                except Exception:
                    pass

                updated_names.append(f"{emp.name} ({emp.emp_id})")

        msg = f"{len(updated_names)} worker(s) successfully marked {'ON LEAVE' if action == 'MARK_LEAVE' else 'RESUMED DUTY'}: {', '.join(updated_names[:3])}"
        if len(updated_names) > 3:
            msg += f" and {len(updated_names) - 3} more."

        return JsonResponse({
            'status': 'success',
            'msg': msg,
            'count': len(updated_names)
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def api_leave_search_worker(request):
    """
    Search endpoint specifically for Leave Management forms with auto-fill data:
    Employee ID, Full Name, Designation, Department, Agency/Contractor, Contact, Room, Block, CID/Permit.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from portal.models import Employee

    q = str(request.GET.get('q', '')).strip()
    if not q:
        emps = Employee.objects.filter(status='Active').order_by('name')[:25]
    else:
        emps = Employee.objects.filter(status='Active').filter(
            Q(emp_id__icontains=q) |
            Q(name__icontains=q) |
            Q(camp_room__icontains=q) |
            Q(contact_info__icontains=q) |
            Q(designation__icontains=q)
        )[:30]

    results = []
    for e in emps:
        r_num = (e.camp_room or '').strip().upper()
        b_code = r_num[0] if r_num and r_num[0].isalpha() else 'N/A'
        results.append({
            'id': e.id,
            'emp_id': e.emp_id or '',
            'name': e.name or '',
            'designation': e.designation or 'Worker',
            'department': e.department or '',
            'agency': e.contractor_agency or 'Company',
            'contact': e.contact_info or '',
            'camp_room': e.camp_room or 'N/A',
            'block_code': b_code,
            'cid_number': e.cid_number or '',
            'nationality': e.nationality or 'Expatriate',
            'is_on_leave': (e.status == 'On Leave' or 'leave' in (e.shift_remarks or '').lower()),
        })

    return JsonResponse({'status': 'SUCCESS', 'workers': results})


@login_required
def api_camp_worker_edit(request):
    """
    Edit resident worker details from Block Explorer / Camp Dashboard.
    """
    from django.http import JsonResponse
    from portal.models import Employee
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        emp_id = data.get('emp_id')
        name = (data.get('name') or '').strip()
        emp_id_code = (data.get('emp_id_code') or '').strip()
        contact_info = (data.get('contact_info') or '').strip()
        designation = (data.get('designation') or '').strip()
        contractor_agency = (data.get('contractor_agency') or '').strip()
        nationality = (data.get('nationality') or '').strip()
        cid_number = (data.get('cid_number') or '').strip()

        emp = Employee.objects.filter(id=emp_id).first()
        if not emp:
            return JsonResponse({'status': 'error', 'msg': 'Worker not found'}, status=404)

        if not name:
            return JsonResponse({'status': 'error', 'msg': 'Worker name cannot be empty'}, status=400)

        if emp_id_code and emp_id_code != emp.emp_id:
            if Employee.objects.filter(emp_id=emp_id_code).exclude(id=emp.id).exists():
                return JsonResponse({'status': 'error', 'msg': f'Employee ID {emp_id_code} is already assigned to another worker.'}, status=400)
            emp.emp_id = emp_id_code

        emp.name = name
        emp.contact_info = contact_info
        if designation:
            emp.designation = designation
        if contractor_agency:
            emp.contractor_agency = contractor_agency
        if nationality:
            emp.nationality = nationality
        if cid_number:
            emp.cid_number = cid_number
            emp.work_permit_no = cid_number

        if request.user.is_authenticated:
            emp.entered_by = request.user
        emp.save()

        updater_name = "Manager Sir"
        if emp.entered_by:
            updater_name = emp.entered_by.get_full_name() or emp.entered_by.username

        return JsonResponse({
            'status': 'success',
            'updated_by': updater_name,
            'msg': f'Details for {emp.name} ({emp.emp_id}) updated successfully!'
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=500)


@login_required
def export_camp_master_allotments_excel(request):
    """
    Exports the Complete Master Camp Room & Bed Allotment Register to Excel (.xlsx).
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from django.http import HttpResponse
    from portal.models import CampBlock, CampRoom, Employee

    wb = openpyxl.Workbook()

    # Sheet 1: Block Summary
    ws_sum = wb.active
    ws_sum.title = 'Block Summary'
    ws_sum.views.sheetView[0].showGridLines = True

    h_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
    h_font = Font(name='Calibri', size=11, bold=True, color='FFFFFF')

    headers_sum = ['Block Code', 'Block Name', 'Category', 'Total Rooms', 'Occupied Rooms', 'Vacant Rooms', 'Residents (Working + Leave)', 'Total Capacity (8/rm)', 'Occupancy %']
    ws_sum.append(headers_sum)

    for col in range(1, len(headers_sum) + 1):
        cell = ws_sum.cell(1, col)
        cell.fill = h_fill
        cell.font = h_font
        cell.alignment = Alignment(horizontal='center', vertical='center')

    grand_rooms = grand_occ = grand_res = grand_cap = 0
    for b in CampBlock.objects.all().order_by('block_code'):
        rooms = list(CampRoom.objects.filter(block=b))
        tot_r = len(rooms)
        occ_r = 0
        tot_res = 0
        for r in rooms:
            c = Employee.objects.filter(camp_room=r.room_number, camp_status='INSIDE').count()
            if c > 0:
                occ_r += 1
                tot_res += c
        vac = tot_r - occ_r
        cap = tot_r * 8
        pct = f'{round(tot_res / cap * 100)}%' if cap else '0%'
        ws_sum.append([b.block_code, b.name, b.category, tot_r, occ_r, vac, tot_res, cap, pct])
        grand_rooms += tot_r
        grand_occ += occ_r
        grand_res += tot_res
        grand_cap += cap

    tot_pct = f'{round(grand_res / grand_cap * 100)}%' if grand_cap else '0%'
    ws_sum.append(['TOTAL', 'All 16 Camp Blocks', '-', grand_rooms, grand_occ, grand_rooms - grand_occ, grand_res, grand_cap, tot_pct])
    last_r = ws_sum.max_row
    for c in range(1, len(headers_sum) + 1):
        cell = ws_sum.cell(last_r, c)
        cell.font = Font(name='Calibri', size=11, bold=True)
        cell.fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')

    # Sheet 2: Master Allotments
    ws_det = wb.create_sheet(title='Master Allotments')
    ws_det.views.sheetView[0].showGridLines = True

    headers_det = ['Block', 'Room Number', 'Bed #', 'Employee ID', 'Full Name', 'Designation', 'Contractor / Agency', 'Bed Cot', 'Mattress', 'Pillow', 'Fan', 'Nationality', 'Status']
    ws_det.append(headers_det)

    for col in range(1, len(headers_det) + 1):
        cell = ws_det.cell(1, col)
        cell.fill = PatternFill(start_color='203764', end_color='203764', fill_type='solid')
        cell.font = h_font
        cell.alignment = Alignment(horizontal='center', vertical='center')

    for b in CampBlock.objects.all().order_by('block_code'):
        rooms = list(CampRoom.objects.filter(block=b))
        rooms.sort(key=lambda x: _natural_sort_key(x.room_number))
        for r in rooms:
            emps = list(Employee.objects.filter(camp_room=r.room_number, camp_status='INSIDE').select_related('allotted_camp_assets').order_by('name'))
            bed_idx = 1
            for emp in emps:
                asset = getattr(emp, 'allotted_camp_assets', None)
                bed_str = asset.bed_number if asset and asset.bed_number else f'Bed {bed_idx}'
                ws_det.append([
                    b.block_code,
                    r.room_number,
                    bed_str,
                    emp.emp_id,
                    emp.name,
                    emp.designation or 'Worker',
                    emp.contractor_agency or 'Company',
                    'Yes' if asset and asset.bed_cot_allotted else 'No',
                    'Yes' if asset and asset.mattress_allotted else 'No',
                    'Yes' if asset and asset.pillow_allotted else 'No',
                    'Yes' if asset and asset.fan_allotted else 'No',
                    emp.nationality or 'Expatriate',
                    emp.shift_remarks or 'Active (Working)'
                ])
                bed_idx += 1

    for ws in [ws_sum, ws_det]:
        for col in ws.columns:
            m_len = max(len(str(c.value or '')) for c in col)
            ws.column_dimensions[col[0].column_letter].width = max(m_len + 3, 12)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="Active_Camp_Room_Allotments.xlsx"'
    wb.save(response)
    return response


def _get_entry_author_user(entry_key):
    try:
        from portal.models import CampMovementLog, MessLog, DailyDeployment, OvertimeRecord
        from fleet.models import RepairLog
        k = str(entry_key).strip()
        if k.startswith('camp_'):
            lid = int(k.split('_')[1])
            obj = CampMovementLog.objects.select_related('scanned_by').filter(id=lid).first()
            return obj.scanned_by if obj else None
        elif k.startswith('mess_'):
            lid = int(k.split('_')[1])
            obj = MessLog.objects.select_related('scanned_by').filter(id=lid).first()
            return obj.scanned_by if obj else None
        elif k.startswith('dep_'):
            lid = int(k.split('_')[1])
            obj = DailyDeployment.objects.select_related('entered_by').filter(id=lid).first()
            return obj.entered_by if obj else None
        elif k.startswith('rep_'):
            lid = int(k.split('_')[1])
            obj = RepairLog.objects.select_related('logged_by').filter(id=lid).first()
            return obj.logged_by if obj else None
        elif k.startswith('ot_'):
            lid = int(k.split('_')[1])
            obj = OvertimeRecord.objects.select_related('time_keeper').filter(id=lid).first()
            return obj.time_keeper if obj else None
    except Exception:
        pass
    return None


@csrf_exempt
@login_required
def api_feed_toggle_like(request):
    """
    Toggle Like/Unlike on a Social Feed Entry Log across any module.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    try:
        from portal.models import FeedEntryLike, CampMovementLog
        data = json.loads(request.body.decode('utf-8'))
        entry_key = str(data.get('entry_key') or data.get('entry_id') or data.get('log_id') or '').strip()
        
        if not entry_key:
            return JsonResponse({'status': 'error', 'message': 'Missing entry key'}, status=400)
            
        log_entry = None
        if entry_key.isdigit():
            log_entry = CampMovementLog.objects.filter(id=int(entry_key)).first()
            entry_key = f"camp_{entry_key}"

        like_qs = FeedEntryLike.objects.filter(user=request.user, entry_key=entry_key)
        if like_qs.exists():
            like_qs.delete()
            is_liked = False
        else:
            FeedEntryLike.objects.create(entry_key=entry_key, log_entry=log_entry, user=request.user)
            is_liked = True

        total_likes = FeedEntryLike.objects.filter(entry_key=entry_key).count()

        if is_liked:
            try:
                author_user = _get_entry_author_user(entry_key)
                if author_user and author_user != request.user:
                    from portal.models import Notification
                    actor_name = request.user.full_name or request.user.username
                    Notification.objects.create(
                        user=author_user,
                        title=f"❤️ {actor_name} liked your entry",
                        message=f"{actor_name} liked your entry in Social Feed.",
                        notification_type='SOCIAL',
                        link=f"/post/{entry_key}/"
                    )
            except Exception:
                pass

        return JsonResponse({
            'status': 'success',
            'is_liked': is_liked,
            'like_count': total_likes
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@csrf_exempt
@login_required
def api_feed_add_comment(request):
    """
    Add a comment to a Social Feed Entry Log across any module.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    try:
        from portal.models import FeedEntryComment, CampMovementLog
        data = json.loads(request.body.decode('utf-8'))
        entry_key = str(data.get('entry_key') or data.get('entry_id') or data.get('log_id') or '').strip()
        text = str(data.get('comment_text', '')).strip()

        if not text:
            return JsonResponse({'status': 'error', 'message': 'Comment text cannot be empty'}, status=400)
        if not entry_key:
            return JsonResponse({'status': 'error', 'message': 'Missing entry key'}, status=400)

        log_entry = None
        if entry_key.isdigit():
            log_entry = CampMovementLog.objects.filter(id=int(entry_key)).first()
            entry_key = f"camp_{entry_key}"

        comment = FeedEntryComment.objects.create(
            entry_key=entry_key,
            log_entry=log_entry,
            user=request.user,
            comment_text=text
        )

        user_name = request.user.full_name or request.user.username
        time_str = timezone.localtime(comment.created_at).strftime('%b %d, %I:%M %p')
        total_comments = FeedEntryComment.objects.filter(entry_key=entry_key).count()

        try:
            author_user = _get_entry_author_user(entry_key)
            if author_user and author_user != request.user:
                from portal.models import Notification
                actor_name = request.user.full_name or request.user.username
                Notification.objects.create(
                    user=author_user,
                    title=f"💬 {actor_name} commented on your entry",
                    message=f'{actor_name}: "{text[:100]}"',
                    notification_type='SOCIAL',
                    link=f"/post/{entry_key}/"
                )
        except Exception:
            pass

        return JsonResponse({
            'status': 'success',
            'comment': {
                'id': comment.id,
                'user_name': user_name,
                'comment_text': comment.comment_text,
                'time': time_str
            },
            'comment_count': total_comments
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@login_required
def api_feed_get_comments(request):
    """
    Fetch comments thread for a Social Feed Entry Log.
    """
    from portal.models import FeedEntryComment
    log_id = request.GET.get('log_id')
    log_entry = get_object_or_404(CampMovementLog, id=log_id)
    comments = []
    for c in log_entry.feed_comments.select_related('user').order_by('created_at'):
        comments.append({
            'id': c.id,
            'user_name': c.user.full_name or c.user.username,
            'comment_text': c.comment_text,
            'time': timezone.localtime(c.created_at).strftime('%b %d, %I:%M %p')
        })

    return JsonResponse({
        'status': 'SUCCESS',
        'comments': comments,
        'comment_count': len(comments)
    })


@login_required
def camp_gate_view(request):
    """
    Security Guard Gate Scanner & In/Out Movement Desk.
    Features: live camera barcode/QR scanner, manual employee search, direction toggle (IN/OUT).
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    is_authorized = (
        request.user.is_superuser or
        request.user.system_role in ['MANAGER', 'PROJECT_MANAGER', 'TIME_KEEPER', 'DEO'] or
        any(m in allowed_modules for m in ['camp', 'camp_guard', 'camp_manager'])
    )
    if not is_authorized:
        messages.error(request, "Permission Denied: Gate Desk is reserved for Security Guards.")
        return redirect('dashboard')

    today = timezone.now().date()
    today_in = CampMovementLog.objects.filter(date=today, direction='IN').count()
    today_out = CampMovementLog.objects.filter(date=today, direction='OUT').count()

    # Movement filters for Real-Time Camp Gate In/Out Log
    selected_dir = request.GET.get('direction', 'ALL')
    selected_gate = request.GET.get('gate_name', 'ALL')
    search_q = request.GET.get('q', '').strip()

    movements_qs = CampMovementLog.objects.select_related('employee', 'scanned_by').all()
    if selected_dir and selected_dir != 'ALL':
        movements_qs = movements_qs.filter(direction=selected_dir)
    if selected_gate and selected_gate != 'ALL':
        movements_qs = movements_qs.filter(gate_name=selected_gate)
    if search_q:
        movements_qs = movements_qs.filter(
            Q(employee__name__icontains=search_q) |
            Q(employee__emp_id__icontains=search_q) |
            Q(employee__department__icontains=search_q)
        )

    recent_movements = list(movements_qs.order_by('-timestamp')[:100])
    emp_ids = [m.employee_id for m in recent_movements]
    all_emp_logs = list(CampMovementLog.objects.filter(employee_id__in=emp_ids).order_by('employee_id', 'timestamp'))
    from collections import defaultdict
    logs_by_emp = defaultdict(list)
    for l in all_emp_logs:
        logs_by_emp[l.employee_id].append(l)

    for m in recent_movements:
        emp_list = logs_by_emp[m.employee_id]
        m_time_str = timezone.localtime(m.timestamp).strftime('%I:%M:%S %p')
        if m.direction == 'OUT':
            m.paired_out_time = m_time_str
            p_ins = [x for x in emp_list if x.direction == 'IN' and x.timestamp < m.timestamp]
            if p_ins:
                last_in = p_ins[-1]
                m.paired_in_time = timezone.localtime(last_in.timestamp).strftime('%I:%M:%S %p')
                sec = (m.timestamp - last_in.timestamp).total_seconds()
                m.duration_str = f"{int(sec//3600)}h {int((sec%3600)//60)}m"
            else:
                m.paired_in_time = "--"
                m.duration_str = "--"
        else:
            m.paired_in_time = m_time_str
            p_outs = [x for x in emp_list if x.direction == 'OUT' and x.timestamp < m.timestamp]
            if p_outs:
                last_out = p_outs[-1]
                m.paired_out_time = timezone.localtime(last_out.timestamp).strftime('%I:%M:%S %p')
                sec = (m.timestamp - last_out.timestamp).total_seconds()
                m.duration_str = f"{int(sec//3600)}h {int((sec%3600)//60)}m"
            else:
                m.paired_out_time = "--"
                m.duration_str = "--"

    gates = list(CampMovementLog.objects.values_list('gate_name', flat=True).distinct())
    gates = [g for g in gates if g] or ['Main Camp Gate']

    active_employees = Employee.objects.filter(status='Active').order_by('name')[:300]
    mess_locations = MessLocation.objects.filter(is_active=True)
    departments = [d for d in Employee.objects.values_list('department', flat=True).distinct() if d]

    return render(request, 'camp_gate.html', {
        'today_punches': recent_movements,
        'recent_movements': recent_movements,
        'today_in': today_in,
        'today_out': today_out,
        'active_employees': active_employees,
        'mess_locations': mess_locations,
        'departments': departments,
        'today_date': today,
        'gates': gates,
        'selected_dir': selected_dir,
        'selected_gate': selected_gate,
        'search_q': search_q,
        'is_manager': _is_camp_manager(request.user),
    })


@csrf_exempt
@login_required
def api_camp_gate_punch(request):
    """
    API endpoint for Security Guards to record a Worker IN or OUT gate movement.
    Accepts QR scan payload or manual form submission.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
        identifier = str(data.get('emp_identifier', '')).strip()
        direction = str(data.get('direction', 'IN')).upper()
        purpose = str(data.get('purpose', 'SHIFT_DUTY')).upper()
        gate_name = str(data.get('gate_name', 'Main Camp Gate')).strip()
        remarks = str(data.get('remarks', '')).strip()

        if not identifier:
            return JsonResponse({'status': 'ERROR', 'msg': 'Please provide an Employee ID or scan QR code.'}, status=400)

        # Intelligent parameter extractor if QR scanned is a URL or query string
        if 'emp_id=' in identifier:
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(identifier)
                params = urllib.parse.parse_qs(parsed.query)
                if 'emp_id' in params:
                    identifier = params['emp_id'][0]
            except Exception:
                pass

        clean_id = identifier
        if ':' in identifier:
            clean_id = identifier.split(':')[-1].strip()

        # Find employee
        emp = None
        if clean_id.isdigit() and len(clean_id) <= 6:
            emp = Employee.objects.filter(id=int(clean_id)).first()

        if not emp:
            emp = Employee.objects.filter(
                Q(emp_id__iexact=clean_id) |
                Q(emp_id__iexact=identifier) |
                Q(name__iexact=clean_id) |
                Q(contact_info__iexact=clean_id) |
                Q(cid_number__iexact=clean_id)
            ).first()

        if not emp:
            emp = Employee.objects.filter(
                Q(emp_id__icontains=clean_id) |
                Q(name__icontains=clean_id)
            ).first()

        if not emp:
            return JsonResponse({'status': 'ERROR', 'msg': f"No worker found matching '{identifier}'."}, status=404)

        if emp.status != 'Active':
            return JsonResponse({'status': 'WARNING', 'msg': f"Worker {emp.name} ({emp.emp_id}) status is '{emp.status}'. Please verify with HR."}, status=400)

        # Record movement with 5-minute rapid punch merge protection
        last_punch = CampMovementLog.objects.filter(employee=emp).order_by('-timestamp').first()
        if last_punch and (timezone.now() - last_punch.timestamp).total_seconds() < 300:
            last_punch.direction = direction
            last_punch.timestamp = timezone.now()
            last_punch.date = timezone.now().date()
            last_punch.gate_name = gate_name or last_punch.gate_name or 'Main Camp Gate'
            last_punch.scanned_by = request.user if request.user.is_authenticated else last_punch.scanned_by
            last_punch.purpose = purpose
            last_punch.remarks = remarks
            last_punch.save()
            punch = last_punch
        else:
            punch = CampMovementLog.objects.create(
                employee=emp,
                direction=direction,
                date=timezone.now().date(),
                gate_name=gate_name or 'Main Camp Gate',
                scanned_by=request.user,
                entry_mode='GUARD_SCAN',
                purpose=purpose,
                remarks=remarks
            )

        # Update real-time camp residence status
        emp.camp_status = 'INSIDE' if direction == 'IN' else 'OUTSIDE'
        emp.save(update_fields=['camp_status'])

        emp_code = f"EMP-{emp.id:04d}" if '@' in emp.emp_id else emp.emp_id
        direction_label = "🟢 ENTERED CAMP" if direction == 'IN' else "🔴 EXITED CAMP"

        now_local = timezone.localtime(punch.timestamp)
        curr_time_str = now_local.strftime('%I:%M:%S %p')
        curr_date_str = now_local.strftime('%Y-%m-%d')

        in_time_str = "--"
        out_time_str = "--"
        duration_str = "--"

        if direction == 'OUT':
            out_time_str = curr_time_str
            prev_in = CampMovementLog.objects.filter(employee=emp, direction='IN', timestamp__lt=punch.timestamp).order_by('-timestamp').first()
            if prev_in:
                in_time_str = timezone.localtime(prev_in.timestamp).strftime('%I:%M:%S %p')
                sec = (punch.timestamp - prev_in.timestamp).total_seconds()
                duration_str = f"{int(sec//3600)}h {int((sec%3600)//60)}m inside"
            else:
                duration_str = "Exited Camp"
        else:
            in_time_str = curr_time_str
            prev_out = CampMovementLog.objects.filter(employee=emp, direction='OUT', timestamp__lt=punch.timestamp).order_by('-timestamp').first()
            if prev_out:
                out_time_str = timezone.localtime(prev_out.timestamp).strftime('%I:%M:%S %p')
                sec = (punch.timestamp - prev_out.timestamp).total_seconds()
                duration_str = f"{int(sec//3600)}h {int((sec%3600)//60)}m outside"
            else:
                duration_str = "Entered Camp"

        return JsonResponse({
            'status': 'SUCCESS',
            'msg': f"{emp.name} ({emp_code}) {direction_label}",
            'worker': {
                'id': emp.id,
                'log_id': punch.id,
                'emp_id': emp_code,
                'name': emp.name,
                'designation': emp.designation or 'P&M Staff',
                'department': emp.department or 'General Site',
                'camp_status': emp.camp_status,
                'camp_room': emp.camp_room or 'Unassigned',
                'direction': direction,
                'date': curr_date_str,
                'time': curr_time_str,
                'in_time': in_time_str,
                'out_time': out_time_str,
                'duration': duration_str,
                'gate_name': punch.gate_name,
                'scanned_by': request.user.get_full_name() or request.user.username if request.user.is_authenticated else 'Guard Scanner',
            }
        })

    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


def _get_active_ngrok_url():
    """
    Attempts to read public URL from local ngrok API (127.0.0.1:4040).
    Returns https public URL or None.
    """
    try:
        import urllib.request, json
        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            tunnels = data.get('tunnels', [])
            for t in tunnels:
                if t.get('proto') == 'https':
                    return t.get('public_url')
            if tunnels:
                return tunnels[0].get('public_url')
    except Exception:
        pass
    return None


@login_required
def camp_gate_poster_view(request):
    """
    Renders printable Camp Gate QR Code Poster for walls / security barriers.
    Auto-detects active ngrok tunnel for seamless mobile QR scanning.
    """
    ngrok_url = _get_active_ngrok_url()
    local_domain = request.build_absolute_uri('/')[:-1]
    is_request_ngrok = 'ngrok' in request.get_host()

    if is_request_ngrok:
        active_url = f"{local_domain}/camp/gate-punch/"
        active_type = 'NGROK'
    elif ngrok_url:
        active_url = f"{ngrok_url}/camp/gate-punch/"
        active_type = 'NGROK'
    else:
        active_url = f"{local_domain}/camp/gate-punch/"
        active_type = 'LOCAL'

    return render(request, 'camp_gate_poster.html', {
        'punch_url': active_url,
        'ngrok_url': f"{ngrok_url}/camp/gate-punch/" if ngrok_url else '',
        'local_url': f"{local_domain}/camp/gate-punch/",
        'has_ngrok': bool(ngrok_url or is_request_ngrok),
        'active_type': active_type,
    })


def _calculate_haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate distance between two GPS points in meters using Haversine formula.
    """
    import math
    R = 6371000  # Radius of earth in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


@login_required
@csrf_exempt
def api_camp_settings(request):
    """
    Get or update Camp Geofencing & Curfew settings.
    Allows manager to pin current location as camp center or update radius.
    """
    from portal.models import CampSetting
    setting, _ = CampSetting.objects.get_or_create(id=1)

    if request.method == 'POST':
        if not _is_camp_manager(request.user):
            return JsonResponse({'status': 'ERROR', 'msg': 'Permission denied.'}, status=403)
        try:
            data = json.loads(request.body.decode('utf-8'))
            if 'latitude' in data and 'longitude' in data:
                setting.latitude = float(data.get('latitude', 0.0))
                setting.longitude = float(data.get('longitude', 0.0))
            if 'geofence_radius_meters' in data:
                setting.geofence_radius_meters = int(data.get('geofence_radius_meters', 350))
            if 'geofence_enabled' in data:
                setting.geofence_enabled = bool(data.get('geofence_enabled'))
            if 'voice_guidance_enabled' in data:
                setting.voice_guidance_enabled = bool(data.get('voice_guidance_enabled'))
            if 'curfew_max_outside_hours' in data:
                setting.curfew_max_outside_hours = int(data.get('curfew_max_outside_hours', 4))
            if 'curfew_start_time' in data:
                t_str = str(data.get('curfew_start_time', '')).strip()
                if t_str:
                    try:
                        setting.curfew_start_time = datetime.strptime(t_str, '%H:%M').time()
                    except Exception:
                        pass
            setting.save()
            return JsonResponse({
                'status': 'SUCCESS',
                'msg': 'Camp settings updated successfully!',
                'settings': {
                    'latitude': setting.latitude,
                    'longitude': setting.longitude,
                    'geofence_radius_meters': setting.geofence_radius_meters,
                    'geofence_enabled': setting.geofence_enabled,
                    'voice_guidance_enabled': setting.voice_guidance_enabled,
                    'curfew_max_outside_hours': setting.curfew_max_outside_hours,
                    'curfew_start_time': setting.curfew_start_time.strftime('%H:%M') if setting.curfew_start_time else '21:30',
                }
            })
        except Exception as e:
            return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=400)

    return JsonResponse({
        'status': 'SUCCESS',
        'settings': {
            'camp_name': setting.camp_name,
            'latitude': setting.latitude,
            'longitude': setting.longitude,
            'geofence_radius_meters': setting.geofence_radius_meters,
            'geofence_enabled': setting.geofence_enabled,
            'voice_guidance_enabled': setting.voice_guidance_enabled,
            'curfew_max_outside_hours': setting.curfew_max_outside_hours,
            'curfew_start_time': setting.curfew_start_time.strftime('%H:%M') if setting.curfew_start_time else '21:30',
        }
    })


@csrf_exempt
def api_camp_lookup_worker(request):
    """
    Real-time lookup endpoint for unauthenticated / guest workers scanning the gate QR.
    Matches employee by ID, badge code, phone or name, returning full profile details.
    """
    q = str(request.GET.get('q', '')).strip()
    if not q or len(q) < 2:
        return JsonResponse({'status': 'ERROR', 'msg': 'Query too short'}, status=400)

    # Check if user accidentally scanned or passed the Gate Wall Poster URL
    if ('/camp/gate-punch' in q or 'ngrok' in q) and 'emp_id=' not in q:
        return JsonResponse({
            'status': 'WARNING',
            'msg': "This is the Gate Wall Poster QR code. Please enter your Employee ID (e.g. 2843) or Phone number."
        }, status=400)

    clean_q = q
    if clean_q.startswith('http://') or clean_q.startswith('https://') or clean_q.startswith('//') or '://' in clean_q:
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(clean_q)
            params = urllib.parse.parse_qs(parsed.query)
            if 'emp_id' in params:
                clean_q = params['emp_id'][0]
            elif 'id' in params:
                clean_q = params['id'][0]
            elif 'q' in params:
                clean_q = params['q'][0]
        except Exception:
            pass
    elif ':' in clean_q:
        clean_q = clean_q.split(':')[-1].strip()

    # 1. Broad multi-match search by name, emp_id, phone, camp_room
    matches_qs = Employee.objects.filter(
        Q(emp_id__icontains=clean_q) |
        Q(name__icontains=clean_q) |
        Q(contact_info__icontains=clean_q) |
        Q(camp_room__icontains=clean_q)
    ).order_by('name')

    if not matches_qs.exists() and clean_q.isdigit():
        matches_qs = Employee.objects.filter(
            Q(id=int(clean_q)) |
            Q(emp_id__endswith=clean_q)
        ).order_by('id')

    if not matches_qs.exists():
        return JsonResponse({'status': 'NOT_FOUND', 'msg': f"No worker record found for '{q}'."})

    workers_list = []
    for emp in matches_qs[:25]:
        emp_code = f"EMP-{emp.id:04d}" if '@' in (emp.emp_id or '') else (emp.emp_id or f"EMP-{emp.id:04d}")
        last_punch = CampMovementLog.objects.filter(employee=emp).order_by('-timestamp').first()
        workers_list.append({
            'id': emp.id,
            'emp_id': emp_code,
            'name': emp.name,
            'designation': emp.designation or 'Staff',
            'department': emp.department or 'General Site',
            'camp_room': emp.camp_room or 'Room Not Assigned',
            'camp_status': emp.camp_status or 'INSIDE',
            'status': emp.status or 'Active',
            'assigned_mess': emp.assigned_mess.name if getattr(emp, 'assigned_mess', None) else 'Main Mess Hall',
            'current_shift': emp.current_shift or 'Day Shift',
            'contractor_agency': emp.contractor_agency or 'Direct / General',
            'last_punch': {
                'direction': last_punch.direction if last_punch else None,
                'direction_display': last_punch.get_direction_display() if last_punch else None,
                'time': last_punch.timestamp.strftime('%I:%M %p') if last_punch else None,
                'date': last_punch.date.strftime('%d %b %Y') if last_punch else None,
            } if last_punch else None
        })

    return JsonResponse({
        'status': 'SUCCESS',
        'worker': workers_list[0] if workers_list else None,
        'workers': workers_list
    })


@csrf_exempt
def camp_gate_self_punch_view(request):
    """
    Worker self-punch view opened upon scanning Gate QR Poster on the wall.
    Allows worker to self-register IN or OUT movement.
    """
    today = timezone.now().date()
    default_emp_id = ''
    employee = None
    if hasattr(request, 'user') and request.user.is_authenticated:
        employee = _get_or_create_user_employee(request.user)
        if employee:
            emp_code = f"EMP-{employee.id:04d}" if '@' in (employee.emp_id or '') else employee.emp_id
            default_emp_id = emp_code

    query_emp = str(request.GET.get('emp_id', '')).strip()
    if query_emp:
        default_emp_id = query_emp
        if not employee:
            emp_match = Employee.objects.filter(
                Q(emp_id__iexact=query_emp) |
                Q(contact_info__iexact=query_emp) |
                Q(name__iexact=query_emp)
            ).first()
            if not emp_match and len(query_emp) >= 3:
                emp_match = Employee.objects.filter(
                    Q(emp_id__endswith=query_emp) |
                    Q(emp_id__endswith='-' + query_emp)
                ).first()
            if emp_match:
                employee = emp_match

    from portal.models import CampSetting
    camp_setting, _ = CampSetting.objects.get_or_create(id=1)

    result = None

    if request.method == 'POST':
        is_json = False
        data = {}
        if request.body and ('application/json' in request.content_type or request.headers.get('Content-Type') == 'application/json'):
            try:
                data = json.loads(request.body.decode('utf-8'))
                is_json = True
            except Exception:
                pass

        if is_json:
            identifier = str(data.get('emp_identifier', '')).strip()
            direction = str(data.get('direction', 'IN')).upper()
            purpose = str(data.get('purpose', 'SHIFT_DUTY')).upper()
            remarks = str(data.get('remarks', '')).strip()
            worker_lat = data.get('lat')
            worker_lon = data.get('lon')
        else:
            identifier = str(request.POST.get('emp_identifier', '')).strip()
            direction = str(request.POST.get('direction', 'IN')).upper()
            purpose = str(request.POST.get('purpose', 'SHIFT_DUTY')).upper()
            remarks = str(request.POST.get('remarks', '')).strip()
            worker_lat = request.POST.get('lat')
            worker_lon = request.POST.get('lon')

        # GEOFENCE VERIFICATION (If Enabled in Camp Settings)
        if camp_setting.geofence_enabled and camp_setting.latitude and camp_setting.longitude:
            try:
                w_lat = float(worker_lat)
                w_lon = float(worker_lon)
                dist_meters = _calculate_haversine_distance(camp_setting.latitude, camp_setting.longitude, w_lat, w_lon)
                if dist_meters > camp_setting.geofence_radius_meters:
                    dist_km = round(dist_meters / 1000.0, 2)
                    result = {
                        'status': 'ERROR',
                        'msg': f"Location Verification Failed: You are {dist_km} km away from Camp. Punch is only permitted within {camp_setting.geofence_radius_meters}m of Camp Gate."
                    }
                    if is_json or request.headers.get('Accept') == 'application/json' or request.headers.get('x-requested-with') == 'XMLHttpRequest':
                        return JsonResponse(result, status=400)
            except (TypeError, ValueError):
                result = {
                    'status': 'ERROR',
                    'msg': "GPS Location Required: Geofencing is enabled. Please allow location access in your browser to punch."
                }
                if is_json or request.headers.get('Accept') == 'application/json' or request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse(result, status=400)

        if not identifier:
            identifier = default_emp_id

        # Check if user accidentally scanned or submitted the Wall Poster QR URL itself
        if ('/camp/gate-punch' in identifier or 'ngrok' in identifier) and 'emp_id=' not in identifier:
            result = {
                'status': 'ERROR',
                'msg': "This is the Gate Wall Poster QR code. Please enter your Employee ID (e.g. 2843) or Phone number."
            }
            if is_json or request.headers.get('Accept') == 'application/json' or request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse(result, status=400)
            return render(request, 'camp_gate_self_punch.html', {
                'default_emp_id': default_emp_id,
                'logged_in_emp': employee,
                'result': result,
                'today_date': today,
                'camp_setting': camp_setting,
            })

        clean_id = identifier
        if clean_id.startswith('http://') or clean_id.startswith('https://') or clean_id.startswith('//') or '://' in clean_id:
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(clean_id)
                params = urllib.parse.parse_qs(parsed.query)
                if 'emp_id' in params:
                    clean_id = params['emp_id'][0]
                elif 'id' in params:
                    clean_id = params['id'][0]
                elif 'q' in params:
                    clean_id = params['q'][0]
            except Exception:
                pass
        elif ':' in clean_id:
            clean_id = clean_id.split(':')[-1].strip()

        emp = None
        # 1. Exact match by emp_id, identifier, contact_info, or full name
        emp = Employee.objects.filter(
            Q(emp_id__iexact=clean_id) |
            Q(emp_id__iexact=identifier) |
            Q(contact_info__iexact=clean_id) |
            Q(name__iexact=clean_id)
        ).first()

        # 2. Match by suffix / last digits (e.g. 4 digits '2843' or '0002')
        if not emp and len(clean_id) >= 3:
            emp = Employee.objects.filter(
                Q(emp_id__endswith=clean_id) |
                Q(emp_id__endswith='-' + clean_id)
            ).first()

        # 3. Numeric ID match (Primary Key)
        if not emp and clean_id.isdigit() and len(clean_id) <= 6:
            emp = Employee.objects.filter(id=int(clean_id)).first()

        # 4. Fallback substring match
        if not emp:
            emp = Employee.objects.filter(
                Q(emp_id__icontains=clean_id) |
                Q(name__icontains=clean_id)
            ).first()

        if not emp:
            result = {'status': 'ERROR', 'msg': f"No worker record found for '{identifier}'."}
        else:
            valid_purposes = ['SHIFT_DUTY', 'PERSONAL', 'MEDICAL', 'LEAVE', 'OTHER']
            log_purpose = purpose if purpose in valid_purposes else 'SHIFT_DUTY'

            # Anti-double punch debounce & merge window (5 minutes / 300 seconds)
            last_punch = CampMovementLog.objects.filter(employee=emp).order_by('-timestamp').first()
            if last_punch and (timezone.now() - last_punch.timestamp).total_seconds() < 300:
                last_punch.direction = direction
                last_punch.timestamp = timezone.now()
                last_punch.date = today
                last_punch.gate_name = 'Gate QR Wall Scanner'
                last_punch.purpose = log_purpose
                last_punch.remarks = remarks if remarks else ("Worker Entry to Camp" if direction == 'IN' else f"Exit Reason: {log_purpose}")
                if hasattr(request, 'user') and request.user.is_authenticated:
                    last_punch.scanned_by = request.user
                last_punch.save()
                punch = last_punch
            else:
                punch = CampMovementLog.objects.create(
                    employee=emp,
                    direction=direction,
                    date=today,
                    gate_name='Gate QR Wall Scanner',
                    scanned_by=request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None,
                    entry_mode='SELF_SCAN',
                    purpose=log_purpose,
                    remarks=remarks if remarks else ("Worker Entry to Camp" if direction == 'IN' else f"Exit Reason: {log_purpose}")
                )

            emp.camp_status = 'INSIDE' if direction == 'IN' else 'OUTSIDE'
            emp.save(update_fields=['camp_status'])

            if hasattr(request, 'user') and request.user.is_authenticated and not request.user.employee:
                try:
                    request.user.employee = emp
                    request.user.save(update_fields=['employee'])
                except Exception:
                    pass

                emp_code = f"EMP-{emp.id:04d}" if '@' in emp.emp_id else emp.emp_id
                result = {
                    'status': 'SUCCESS',
                    'emp_id': emp_code,
                    'name': emp.name,
                    'designation': emp.designation or 'Staff',
                    'direction': direction,
                    'purpose': punch.purpose,
                    'purpose_display': punch.get_purpose_display(),
                    'time': timezone.localtime(punch.timestamp).strftime('%I:%M:%S %p'),
                    'camp_status': emp.camp_status,
                    'voice_enabled': False,
                }

        if is_json or request.headers.get('Accept') == 'application/json' or request.headers.get('x-requested-with') == 'XMLHttpRequest':
            status_code = 200 if result.get('status') == 'SUCCESS' else 400
            return JsonResponse(result, status=status_code)

    return render(request, 'camp_gate_self_punch.html', {
        'default_emp_id': default_emp_id,
        'logged_in_emp': employee,
        'result': result,
        'today_date': today,
        'camp_setting': camp_setting,
    })


@login_required
def camp_user_view(request):
    """
    Resident Worker Camp Mobile Portal.
    Shows current location status (Inside vs Outside Camp), quick Gate Self-Scan,
    and Mess meal status.
    """
    emp = _get_or_create_user_employee(request.user)
    today = timezone.now().date()

    last_gate_movement = CampMovementLog.objects.filter(employee=emp).order_by('-timestamp').first()
    active_meal, timing_text, current_status_obj = _get_active_meal_window_and_timing(emp)
    today_pass = MessLog.objects.filter(employee=emp, date=today, meal_type=active_meal, status='SUCCESS').first()
    menu_obj = MessMenu.objects.filter(date=today).first()

    return render(request, 'camp_user.html', {
        'emp': emp,
        'last_movement': last_gate_movement,
        'active_meal': active_meal,
        'timing_text': timing_text,
        'current_status': current_status_obj,
        'today_pass': today_pass,
        'menu': menu_obj,
        'today_date': today,
    })


@login_required
def camp_hr_view(request):
    """
    Camp HR Representative Desk.
    Features:
    - Add New Worker (Joinee) with Camp Room/Block allocation
    - Deactivate / Remove Worker (Mark Left Company with exit reason)
    - Active and Exited Worker Roster tables with live filters
    """
    allowed_modules = getattr(request.user, 'assigned_modules', []) or []
    is_authorized = (
        request.user.is_superuser or
        request.user.system_role in ['MANAGER', 'PROJECT_MANAGER'] or
        any(m in allowed_modules for m in ['camp', 'camp_hr', 'camp_manager'])
    )
    if not is_authorized:
        messages.error(request, "Permission Denied: HR Representative Desk is restricted.")
        return redirect('dashboard')

    active_count = Employee.objects.filter(status='Active').count()
    leave_count = Employee.objects.filter(status='On Leave').count()
    terminated_count = Employee.objects.filter(status='Terminated').count()
    inside_count = Employee.objects.filter(status='Active', camp_status='INSIDE').count()
    outside_count = Employee.objects.filter(status='Active', camp_status='OUTSIDE').count()

    selected_status = request.GET.get('status', 'Active')
    selected_dept = request.GET.get('department', 'ALL')
    search_q = request.GET.get('q', '').strip()

    workers_qs = Employee.objects.select_related('assigned_mess').all()

    if selected_status and selected_status != 'ALL':
        workers_qs = workers_qs.filter(status=selected_status)
    if selected_dept and selected_dept != 'ALL':
        workers_qs = workers_qs.filter(department=selected_dept)
    if search_q:
        workers_qs = workers_qs.filter(
            Q(name__icontains=search_q) |
            Q(emp_id__icontains=search_q) |
            Q(contact_info__icontains=search_q) |
            Q(camp_room__icontains=search_q)
        )

    workers = workers_qs.order_by('name')[:150]
    departments = list(Employee.objects.values_list('department', flat=True).distinct())
    departments = [d for d in departments if d]
    mess_locations = MessLocation.objects.filter(is_active=True)

    return render(request, 'camp_hr.html', {
        'workers': workers,
        'active_count': active_count,
        'leave_count': leave_count,
        'terminated_count': terminated_count,
        'inside_count': inside_count,
        'outside_count': outside_count,
        'departments': departments,
        'mess_locations': mess_locations,
        'selected_status': selected_status,
        'selected_dept': selected_dept,
        'search_q': search_q,
    })


@csrf_exempt
@login_required
def api_hr_add_employee(request):
    """
    API endpoint for HR Representative to onboard a new employee / worker.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
        emp_id = str(data.get('emp_id', '')).strip()
        name = str(data.get('name', '')).strip()
        designation = str(data.get('designation', 'Staff')).strip()
        department = str(data.get('department', 'General Site')).strip()
        phone = str(data.get('contact_info', '')).strip()
        shift = str(data.get('current_shift', 'Day')).strip()
        camp_room = str(data.get('camp_room', '')).strip()
        mess_id = data.get('assigned_mess_id')
        nationality = str(data.get('nationality', 'Indian')).strip()
        contractor = str(data.get('contractor_agency', '')).strip()

        if not emp_id or not name:
            return JsonResponse({'status': 'ERROR', 'msg': 'Employee ID and Full Name are required.'}, status=400)

        # Check duplicate emp_id
        if Employee.objects.filter(emp_id__iexact=emp_id).exists():
            return JsonResponse({'status': 'ERROR', 'msg': f"An employee with ID '{emp_id}' already exists."}, status=400)

        assigned_mess = MessLocation.objects.filter(id=mess_id).first() if mess_id else None

        new_emp = Employee.objects.create(
            emp_id=emp_id,
            name=name,
            designation=designation,
            department=department,
            contact_info=phone,
            current_shift=shift,
            camp_room=camp_room,
            camp_status='INSIDE',
            assigned_mess=assigned_mess,
            nationality=nationality,
            contractor_agency=contractor,
            status='Active',
            entered_by=request.user
        )

        log_activity(request.user, 'CREATE', 'Camp HR', f"Onboarded new worker {name} ({emp_id}) in Camp.", request)

        # Automatically log initial camp entry movement
        CampMovementLog.objects.create(
            employee=new_emp,
            direction='IN',
            date=timezone.now().date(),
            gate_name='Camp HR Joinee Desk',
            scanned_by=request.user,
            entry_mode='HR_ONBOARD',
            purpose='SHIFT_DUTY',
            remarks='New Joinee Onboarded to Camp'
        )

        return JsonResponse({'status': 'SUCCESS', 'msg': f"Worker '{name}' ({emp_id}) added successfully to Camp Roster!"})

    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def api_camp_live_stats(request):
    """
    Unified high-performance real-time JSON feed for Camp Ecosystem.
    Interlinks:
    - Live Camp Headcounts (Inside, Outside, Leave, Terminated, Active)
    - Today's In/Out movement counts & recent 25 gate logs
    - Mess correlation: Total meals fed today, active meal window & kitchen cooking status
    - Current user resident status (Inside/Outside, room, last gate punch)
    - Active workforce room and status map for HR desk
    """
    today = timezone.localdate()

    # 1. Live Headcounts
    inside_count = Employee.objects.filter(status='Active', camp_status='INSIDE').count()
    outside_count = Employee.objects.filter(status='Active', camp_status='OUTSIDE').count()
    leave_count = Employee.objects.filter(status='On Leave').count()
    terminated_count = Employee.objects.filter(status='Terminated').count()
    active_count = Employee.objects.filter(status='Active').count()

    # 2. Today's Movements
    today_movements = CampMovementLog.objects.filter(date=today)
    in_today = today_movements.filter(direction='IN').count()
    out_today = today_movements.filter(direction='OUT').count()
    total_today = today_movements.count()

    # 3. Mess Stats Today
    mess_fed_today = MessLog.objects.filter(date=today, status='SUCCESS').count()

    # 4. Recent Movement Logs (latest 25 with rapid punch consolidation)
    recent_logs = []
    raw_logs = list(CampMovementLog.objects.select_related('employee', 'scanned_by').order_by('-timestamp')[:60])
    filtered_logs = []
    seen_emp_times = {}

    for l in raw_logs:
        emp_id = l.employee_id
        if emp_id in seen_emp_times:
            prev_ts = seen_emp_times[emp_id]
            if abs((prev_ts - l.timestamp).total_seconds()) < 300:
                continue
        seen_emp_times[emp_id] = l.timestamp
        filtered_logs.append(l)

    for l in filtered_logs[:25]:
        emp_code = f"EMP-{l.employee.id:04d}" if '@' in l.employee.emp_id else l.employee.emp_id
        guard_name = (l.scanned_by.get_full_name() or l.scanned_by.username) if l.scanned_by else "Self Scan / Wall QR"
        local_ts = timezone.localtime(l.timestamp) if l.timestamp else None
        recent_logs.append({
            'id': l.id,
            'date': l.date.strftime('%Y-%m-%d') if l.date else '',
            'time': local_ts.strftime('%I:%M:%S %p') if local_ts else '',
            'time_short': local_ts.strftime('%H:%M:%S') if local_ts else '',
            'emp_id': emp_code,
            'employee_name': l.employee.name,
            'designation': l.employee.designation or 'Staff',
            'department': l.employee.department or 'General',
            'camp_room': l.employee.camp_room or '-',
            'direction': l.direction,
            'direction_display': l.get_direction_display(),
            'gate_name': l.gate_name,
            'purpose': l.get_purpose_display(),
            'remarks': l.remarks or '',
            'scanned_by': guard_name,
            'entry_mode': l.entry_mode
        })

    # 5. Kitchen Status
    emp_user = _get_or_create_user_employee(request.user)
    active_meal, timing_text, current_status_obj = _get_active_meal_window_and_timing(emp_user)
    kitchen_info = {
        'active_meal': active_meal,
        'timing_text': timing_text,
        'status': current_status_obj.status if current_status_obj else 'COOKING',
        'updated_at': current_status_obj.updated_at.strftime('%I:%M %p') if (current_status_obj and current_status_obj.updated_at) else 'Recently'
    }

    # 6. Current User Specific Live Status
    user_status = None
    if emp_user:
        last_move = CampMovementLog.objects.filter(employee=emp_user).order_by('-timestamp').first()
        user_status = {
            'camp_status': emp_user.camp_status,
            'status': emp_user.status,
            'camp_room': emp_user.camp_room or 'Room Not Assigned',
            'last_movement': {
                'direction': last_move.get_direction_display() if last_move else None,
                'time': last_move.timestamp.strftime('%I:%M %p') if (last_move and last_move.timestamp) else None,
                'gate': last_move.gate_name if last_move else None,
                'purpose': last_move.get_purpose_display() if last_move else None,
            } if last_move else None
        }

    # 7. Workers status mapping (for HR desk live badge sync)
    worker_status_map = {}
    for e in Employee.objects.values('id', 'emp_id', 'camp_status', 'status', 'camp_room')[:250]:
        code = f"EMP-{e['id']:04d}" if '@' in e['emp_id'] else e['emp_id']
        worker_status_map[str(e['id'])] = {
            'emp_id': code,
            'camp_status': e['camp_status'],
            'status': e['status'],
            'camp_room': e['camp_room'] or 'Unassigned'
        }

    # 8. Overstay Alert List & Predictive Mess Meals
    from portal.models import CampSetting
    camp_setting, _ = CampSetting.objects.get_or_create(id=1)
    overstay_list, predictive_meals = _get_camp_curfew_and_predictive_data(camp_setting)

    # 9. On Leave & Absent Worker List
    on_leave_list = []
    leave_emps = Employee.objects.filter(
        Q(status='On Leave') | Q(status='Absent') | Q(camp_status='ON_LEAVE')
    ).select_related('assigned_mess').order_by('name')

    for le in leave_emps:
        emp_code = f"EMP-{le.id:04d}" if '@' in (le.emp_id or '') else (le.emp_id or f"EMP-{le.id:04d}")
        last_out_log = CampMovementLog.objects.filter(employee=le, direction='OUT').order_by('-timestamp').first()
        local_out_ts = timezone.localtime(last_out_log.timestamp) if (last_out_log and last_out_log.timestamp) else None
        on_leave_list.append({
            'id': le.id,
            'emp_id': emp_code,
            'name': le.name,
            'designation': le.designation or 'Staff',
            'department': le.department or 'General',
            'contact_info': le.contact_info or 'N/A',
            'camp_room': le.camp_room or 'Room Unassigned',
            'assigned_mess': le.assigned_mess.name if getattr(le, 'assigned_mess', None) else 'Main Mess Hall',
            'status': le.status or 'On Leave',
            'camp_status': le.camp_status or 'OUTSIDE',
            'out_time': local_out_ts.strftime('%d %b %Y, %I:%M %p') if local_out_ts else 'N/A',
            'remarks': last_out_log.remarks if (last_out_log and last_out_log.remarks) else 'Marked On Leave',
            'gate_name': last_out_log.gate_name if last_out_log else 'HR Desk',
        })

    return JsonResponse({
        'status': 'success',
        'kpis': {
            'inside_count': inside_count,
            'outside_count': outside_count,
            'leave_count': len(on_leave_list) if on_leave_list else leave_count,
            'terminated_count': terminated_count,
            'active_count': active_count,
            'in_today': in_today,
            'out_today': out_today,
            'total_movements_today': total_today,
            'mess_fed_today': mess_fed_today,
        },
        'kitchen': kitchen_info,
        'user_status': user_status,
        'recent_movements': recent_logs,
        'workers_map': worker_status_map,
        'overstay_count': len(overstay_list),
        'overstay_list': overstay_list[:50],
        'predictive_meals': predictive_meals,
        'on_leave_list': on_leave_list,
        'server_time': timezone.now().strftime('%H:%M:%S')
    })


@csrf_exempt
@login_required
def api_hr_deactivate_employee(request):
    """
    API endpoint for HR Representative to mark an employee as Left Company / Terminated.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
        emp_id = data.get('employee_id')
        exit_date_str = data.get('exit_date')
        exit_reason = str(data.get('exit_reason', 'Left Company')).strip()

        emp = get_object_or_404(Employee, id=emp_id)

        exit_d = timezone.now().date()
        if exit_date_str:
            try:
                exit_d = datetime.strptime(exit_date_str, '%Y-%m-%d').date()
            except Exception:
                pass

        emp.status = 'Terminated'
        emp.camp_status = 'OUTSIDE'
        emp.exit_date = exit_d
        emp.exit_reason = exit_reason
        emp.save()

        # Log Gate exit if currently active
        CampMovementLog.objects.create(
            employee=emp,
            direction='OUT',
            date=exit_d,
            gate_name='HR Exit Processing',
            scanned_by=request.user,
            entry_mode='HR_EXIT',
            purpose='LEAVE',
            remarks=f"Exit recorded by HR: {exit_reason}"
        )

        log_activity(request.user, 'UPDATE', 'Camp HR', f"Deactivated/Offboarded worker {emp.name} ({emp.emp_id}). Reason: {exit_reason}", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': f"Worker '{emp.name}' marked as Exited/Left Company successfully."})

    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@csrf_exempt
@login_required
def api_hr_mark_leave(request):
    """
    API endpoint for HR Representative to mark an employee On Leave or Return them to Active status.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'ERROR', 'msg': 'Invalid HTTP method'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
        emp_id = data.get('employee_id')
        action_type = str(data.get('action_type', 'MARK_LEAVE')).upper() # MARK_LEAVE or RETURN_FROM_LEAVE
        leave_reason = str(data.get('leave_reason', 'Home Leave / Travel')).strip()
        start_date_str = data.get('start_date')
        expected_return_str = data.get('expected_return_date')

        emp = get_object_or_404(Employee, id=emp_id)

        today = timezone.now().date()

        from portal.models import EmployeeAttendance

        if action_type == 'MARK_LEAVE':
            emp.status = 'On Leave'
            emp.camp_status = 'OUTSIDE'
            emp.save(update_fields=['status', 'camp_status'])

            # Record gate exit movement as Leave
            CampMovementLog.objects.create(
                employee=emp,
                direction='OUT',
                date=today,
                gate_name='Camp HR Leave Desk',
                scanned_by=request.user,
                entry_mode='HR_LEAVE',
                purpose='LEAVE',
                remarks=f"Authorized Leave: {leave_reason} (Expected return: {expected_return_str or 'Not specified'})"
            )

            # Sync daily attendance record
            EmployeeAttendance.objects.update_or_create(
                employee=emp,
                date=today,
                defaults={'status': 'On Leave', 'punch_source': 'MANUAL'}
            )

            log_activity(request.user, 'UPDATE', 'Camp HR', f"HR marked worker {emp.name} ({emp.emp_id}) as On Leave. Reason: {leave_reason}", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"Worker '{emp.name}' has been marked On Leave successfully."})

        elif action_type == 'MARK_ABSENT':
            emp.status = 'Absent'
            emp.camp_status = 'OUTSIDE'
            emp.save(update_fields=['status', 'camp_status'])

            # Record gate exit movement as Absent
            CampMovementLog.objects.create(
                employee=emp,
                direction='OUT',
                date=today,
                gate_name='Camp HR Leave Desk',
                scanned_by=request.user,
                entry_mode='HR_ABSENT',
                purpose='ABSENT',
                remarks=f"Marked Absent by HR: {leave_reason}"
            )

            # Sync daily attendance record
            EmployeeAttendance.objects.update_or_create(
                employee=emp,
                date=today,
                defaults={'status': 'Absent', 'punch_source': 'MANUAL'}
            )

            log_activity(request.user, 'UPDATE', 'Camp HR', f"HR marked worker {emp.name} ({emp.emp_id}) as Absent. Reason: {leave_reason}", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"Worker '{emp.name}' has been marked Absent successfully."})

        elif action_type in ['RETURN_FROM_LEAVE', 'RETURN_TO_DUTY']:
            emp.status = 'Active'
            emp.camp_status = 'INSIDE'
            emp.save(update_fields=['status', 'camp_status'])

            # Record gate entry movement returning from leave/absent
            CampMovementLog.objects.create(
                employee=emp,
                direction='IN',
                date=today,
                gate_name='Camp HR Leave Desk',
                scanned_by=request.user,
                entry_mode='HR_LEAVE_RETURN',
                purpose='SHIFT_DUTY',
                remarks='Returned to Active Camp Status'
            )

            # Sync daily attendance record
            EmployeeAttendance.objects.update_or_create(
                employee=emp,
                date=today,
                defaults={'status': 'Present', 'punch_source': 'MANUAL'}
            )

            log_activity(request.user, 'UPDATE', 'Camp HR', f"HR reactivated worker {emp.name} ({emp.emp_id}) from Leave/Absent to Active Inside Camp.", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"Worker '{emp.name}' marked Active and returned to Camp successfully."})

        else:
            return JsonResponse({'status': 'ERROR', 'msg': 'Invalid action type.'}, status=400)

    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=500)


@login_required
def camp_reports_view(request):
    """
    Gate In/Out Movements & Camp Headcount Report Hub.
    """
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    selected_dir = request.GET.get('direction', 'ALL')
    selected_dept = request.GET.get('department', 'ALL')

    logs = CampMovementLog.objects.select_related('employee', 'scanned_by').all()

    if from_date: logs = logs.filter(date__gte=from_date)
    if to_date: logs = logs.filter(date__lte=to_date)
    if selected_dir and selected_dir != 'ALL': logs = logs.filter(direction=selected_dir)
    if selected_dept and selected_dept != 'ALL': logs = logs.filter(employee__department__icontains=selected_dept)

    logs = logs.order_by('-timestamp')
    departments = list(Employee.objects.values_list('department', flat=True).distinct())
    departments = [d for d in departments if d]

    return render(request, 'camp_reports.html', {
        'logs': logs[:500],
        'total_count': logs.count(),
        'from_date': from_date or '',
        'to_date': to_date or '',
        'selected_dir': selected_dir,
        'selected_dept': selected_dept,
        'departments': departments,
        'is_manager': _is_camp_manager(request.user),
    })


@csrf_exempt
@login_required
def api_camp_movement_edit(request, log_id):
    """
    Allows Authorized Camp Managers to edit an existing Gate Movement punch entry.
    """
    if not _is_camp_manager(request.user):
        return JsonResponse({'status': 'FORBIDDEN', 'msg': 'Permission Denied: Only Managers can edit movement records.'}, status=403)

    log = get_object_or_404(CampMovementLog, id=log_id)

    if request.method == 'GET':
        return JsonResponse({
            'status': 'SUCCESS',
            'log': {
                'id': log.id,
                'worker_name': log.employee.name,
                'emp_id': f"EMP-{log.employee.id:04d}" if '@' in (log.employee.emp_id or '') else log.employee.emp_id,
                'direction': log.direction,
                'date': log.date.strftime('%Y-%m-%d'),
                'time': log.timestamp.strftime('%H:%M'),
                'purpose': log.purpose,
                'remarks': log.remarks or '',
                'gate_name': log.gate_name or 'Main Camp Gate',
            }
        })

    if request.method == 'POST':
        try:
            data = {}
            if request.body and ('application/json' in request.content_type or request.headers.get('Content-Type') == 'application/json'):
                data = json.loads(request.body.decode('utf-8'))
            else:
                data = request.POST

            new_dir = str(data.get('direction', log.direction)).upper()
            new_purpose = str(data.get('purpose', log.purpose)).upper()
            new_remarks = str(data.get('remarks', log.remarks or '')).strip()
            new_gate = str(data.get('gate_name', log.gate_name or 'Main Camp Gate')).strip()
            new_date_str = str(data.get('date', '')).strip()
            new_time_str = str(data.get('time', '')).strip()

            if new_dir in ['IN', 'OUT']:
                log.direction = new_dir
            if new_purpose in ['SHIFT_DUTY', 'PERSONAL', 'MEDICAL', 'LEAVE', 'OTHER']:
                log.purpose = new_purpose
            log.remarks = new_remarks
            if new_gate:
                log.gate_name = new_gate

            if new_date_str:
                try:
                    log.date = datetime.strptime(new_date_str, '%Y-%m-%d').date()
                except Exception:
                    pass
            if new_date_str and new_time_str:
                try:
                    dt_combined = datetime.strptime(f"{new_date_str} {new_time_str}", '%Y-%m-%d %H:%M')
                    dt_aware = timezone.make_aware(dt_combined) if timezone.is_naive(dt_combined) else dt_combined
                    log.timestamp = dt_aware
                except Exception:
                    pass

            log.save()

            # Synchronize worker's camp_status with their latest punch
            latest_punch = CampMovementLog.objects.filter(employee=log.employee).order_by('-timestamp').first()
            if latest_punch:
                log.employee.camp_status = 'INSIDE' if latest_punch.direction == 'IN' else 'OUTSIDE'
                log.employee.save(update_fields=['camp_status'])

            log_activity(request.user, 'UPDATE', 'Camp Movement', f"Manager edited movement entry #{log.id} for {log.employee.name} to {log.direction} ({log.purpose})", request)

            return JsonResponse({
                'status': 'SUCCESS',
                'msg': f"Movement record for {log.employee.name} updated successfully.",
                'log': {
                    'id': log.id,
                    'direction': log.direction,
                    'direction_display': log.get_direction_display(),
                    'purpose': log.purpose,
                    'purpose_display': log.get_purpose_display(),
                    'remarks': log.remarks,
                    'gate_name': log.gate_name,
                    'date': log.date.strftime('%Y-%m-%d'),
                    'time': log.timestamp.strftime('%H:%M:%S'),
                    'time_display': log.timestamp.strftime('%I:%M %p'),
                    'camp_status': log.employee.camp_status,
                }
            })
        except Exception as e:
            return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=400)

    return JsonResponse({'status': 'ERROR', 'msg': 'Invalid request method'}, status=405)


@csrf_exempt
@login_required
def api_camp_movement_delete(request, log_id):
    """
    Allows Authorized Camp Managers to delete an incorrect Gate Movement punch entry.
    """
    if not _is_camp_manager(request.user):
        return JsonResponse({'status': 'FORBIDDEN', 'msg': 'Permission Denied: Only Managers can delete movement records.'}, status=403)

    log = get_object_or_404(CampMovementLog, id=log_id)
    emp = log.employee
    emp_name = emp.name

    try:
        log.delete()

        # Re-evaluate worker's camp_status based on their new latest punch
        latest_punch = CampMovementLog.objects.filter(employee=emp).order_by('-timestamp').first()
        if latest_punch:
            emp.camp_status = 'INSIDE' if latest_punch.direction == 'IN' else 'OUTSIDE'
        else:
            emp.camp_status = 'INSIDE'
        emp.save(update_fields=['camp_status'])

        log_activity(request.user, 'DELETE', 'Camp Movement', f"Manager deleted movement entry #{log_id} for {emp_name}", request)

        return JsonResponse({
            'status': 'SUCCESS',
            'msg': f"Movement entry for {emp_name} deleted successfully.",
            'new_camp_status': emp.camp_status
        })
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'msg': str(e)}, status=400)


@login_required
def export_camp_excel(request):
    """
    Exports Camp Gate In/Out Movements to Excel (.xlsx).
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    selected_dir = request.GET.get('direction', 'ALL')
    selected_dept = request.GET.get('department', 'ALL')
    selected_gate = request.GET.get('gate_name', 'ALL')
    selected_purpose = request.GET.get('purpose', 'ALL')
    search_q = request.GET.get('q', '').strip()

    logs = CampMovementLog.objects.select_related('employee', 'scanned_by').all()

    if from_date: logs = logs.filter(date__gte=from_date)
    if to_date: logs = logs.filter(date__lte=to_date)
    if selected_dir and selected_dir != 'ALL': logs = logs.filter(direction=selected_dir)
    if selected_dept and selected_dept != 'ALL': logs = logs.filter(employee__department__icontains=selected_dept)
    if selected_gate and selected_gate != 'ALL': logs = logs.filter(gate_name__icontains=selected_gate)
    if selected_purpose and selected_purpose != 'ALL': logs = logs.filter(purpose=selected_purpose)
    if search_q:
        logs = logs.filter(Q(employee__name__icontains=search_q) | Q(employee__emp_id__icontains=search_q))

    logs = logs.order_by('-timestamp')[:5000]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Gate In-Out Movements"

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    headers = ["DATE", "TIME", "EMP ID", "WORKER NAME", "DESIGNATION", "DEPARTMENT", "DIRECTION", "GATE", "PURPOSE", "ENTRY MODE", "SECURITY GUARD", "REMARKS"]
    ws.append(headers)

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for l in logs:
        emp_code = f"EMP-{l.employee.id:04d}" if '@' in l.employee.emp_id else l.employee.emp_id
        guard_name = (l.scanned_by.get_full_name() or l.scanned_by.username) if l.scanned_by else "Self Scan / QR Wall"
        local_ts = timezone.localtime(l.timestamp) if l.timestamp else None

        ws.append([
            l.date.strftime('%Y-%m-%d') if l.date else '-',
            local_ts.strftime('%H:%M:%S') if local_ts else '-',
            emp_code,
            l.employee.name,
            l.employee.designation or 'Staff',
            l.employee.department or '-',
            l.get_direction_display(),
            l.gate_name,
            l.get_purpose_display(),
            l.entry_mode,
            guard_name,
            l.remarks or ''
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=camp_gate_movements_report.xlsx'
    wb.save(response)
    return response


@login_required
def export_camp_pdf(request):
    """
    Renders printable landscape report of Camp Gate Movements.
    """
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    selected_dir = request.GET.get('direction', 'ALL')
    selected_dept = request.GET.get('department', 'ALL')
    selected_gate = request.GET.get('gate_name', 'ALL')
    selected_purpose = request.GET.get('purpose', 'ALL')
    search_q = request.GET.get('q', '').strip()

    logs = CampMovementLog.objects.select_related('employee', 'scanned_by').all()

    if from_date: logs = logs.filter(date__gte=from_date)
    if to_date: logs = logs.filter(date__lte=to_date)
    if selected_dir and selected_dir != 'ALL': logs = logs.filter(direction=selected_dir)
    if selected_dept and selected_dept != 'ALL': logs = logs.filter(employee__department__icontains=selected_dept)
    if selected_gate and selected_gate != 'ALL': logs = logs.filter(gate_name__icontains=selected_gate)
    if selected_purpose and selected_purpose != 'ALL': logs = logs.filter(purpose=selected_purpose)
    if search_q:
        logs = logs.filter(Q(employee__name__icontains=search_q) | Q(employee__emp_id__icontains=search_q))

    logs = logs.order_by('-timestamp')[:2000]

    rows_html = []
    for l in logs:
        emp_code = f"EMP-{l.employee.id:04d}" if '@' in l.employee.emp_id else l.employee.emp_id
        guard_name = (l.scanned_by.get_full_name() or l.scanned_by.username) if l.scanned_by else "Self Scan"
        dir_color = '#16a34a' if l.direction == 'IN' else '#dc2626'
        local_ts = timezone.localtime(l.timestamp) if l.timestamp else None

        rows_html.append(f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 6px 8px;">{l.date.strftime('%Y-%m-%d') if l.date else ''} <span style="color:#64748b;">{local_ts.strftime('%H:%M:%S') if local_ts else ''}</span></td>
            <td style="padding: 6px 8px; font-weight: bold; color:#2563eb;">{emp_code}</td>
            <td style="padding: 6px 8px; font-weight: bold;">{l.employee.name}<br><small style="color:#64748b; font-weight:normal;">{l.employee.designation or 'Staff'}</small></td>
            <td style="padding: 6px 8px;">{l.employee.department or '-'}</td>
            <td style="padding: 6px 8px; font-weight: bold; color:{dir_color};">{l.get_direction_display()}</td>
            <td style="padding: 6px 8px;">{l.gate_name}</td>
            <td style="padding: 6px 8px;">{l.get_purpose_display()}</td>
            <td style="padding: 6px 8px;">👤 {guard_name}</td>
        </tr>
        """)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Camp Security Gate Movements Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; color: #1e293b; }}
            h2 {{ color: #0f172a; border-bottom: 2px solid #2563eb; padding-bottom: 8px; margin-bottom: 4px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 11px; }}
            th {{ background: #1e293b; color: white; padding: 8px; text-align: left; font-size: 11px; }}
            @media print {{ @page {{ size: landscape; margin: 10mm; }} }}
        </style>
    </head>
    <body>
        <h2>🛡️ Camp Gate Security &amp; In-Out Movements Report</h2>
        <p style="font-size: 12px; color: #64748b; margin: 0 0 12px 0;">Generated on: {timezone.now().strftime('%d %b %Y, %H:%M:%S')} | Total Movements: {logs.count()}</p>
        <table>
            <thead>
                <tr>
                    <th>Date &amp; Time</th>
                    <th>Emp ID</th>
                    <th>Worker Name</th>
                    <th>Department</th>
                    <th>Direction</th>
                    <th>Gate</th>
                    <th>Purpose</th>
                    <th>Security Guard</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows_html)}
            </tbody>
        </table>
        <script>window.onload = function() {{ window.print(); }}</script>
    </body>
    </html>
    """
    return HttpResponse(html)


@login_required
def export_camp_hr_excel(request):
    """
    Exports Camp HR Workforce Roster to Excel (.xlsx).
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    selected_status = request.GET.get('status', 'ALL')
    selected_camp_status = request.GET.get('camp_status', 'ALL')
    selected_dept = request.GET.get('department', 'ALL')
    search_q = request.GET.get('q', '').strip()

    workers = Employee.objects.all().select_related('assigned_mess')

    if selected_status and selected_status != 'ALL':
        workers = workers.filter(status=selected_status)
    if selected_camp_status and selected_camp_status != 'ALL':
        workers = workers.filter(camp_status=selected_camp_status)
    if selected_dept and selected_dept != 'ALL':
        workers = workers.filter(department__icontains=selected_dept)
    if search_q:
        workers = workers.filter(
            Q(name__icontains=search_q) |
            Q(emp_id__icontains=search_q) |
            Q(camp_room__icontains=search_q) |
            Q(contact_info__icontains=search_q)
        )

    workers = workers.order_by('department', 'name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Camp Workforce Roster"

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    headers = [
        "EMP ID", "WORKER NAME", "DESIGNATION", "DEPARTMENT",
        "CAMP PRESENCE", "CAMP ROOM / BLOCK", "WORKER STATUS",
        "CONTACT / PHONE", "ASSIGNED MESS", "JOINING DATE", "EXIT DATE", "REMARKS"
    ]
    ws.append(headers)

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for emp in workers:
        emp_code = f"EMP-{emp.id:04d}" if '@' in (emp.emp_id or '') else (emp.emp_id or f"EMP-{emp.id:04d}")
        camp_display = "Inside Camp" if emp.camp_status == 'INSIDE' else ("Outside / On-Duty" if emp.camp_status == 'OUTSIDE' else "On Leave")
        mess_name = emp.assigned_mess.name if emp.assigned_mess else "Main Mess"

        ws.append([
            emp_code,
            emp.name or '-',
            emp.designation or 'Staff',
            emp.department or 'General',
            camp_display,
            emp.camp_room or 'Unassigned',
            emp.status or 'Active',
            emp.contact_info or '-',
            mess_name,
            emp.joining_date.strftime('%Y-%m-%d') if emp.joining_date else '-',
            emp.exit_date.strftime('%Y-%m-%d') if emp.exit_date else '-',
            emp.exit_reason or ''
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 13)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=camp_workforce_roster.xlsx'
    wb.save(response)
    return response


@login_required
def export_camp_hr_pdf(request):
    """
    Renders printable landscape report of Camp HR Workforce Roster.
    """
    selected_status = request.GET.get('status', 'ALL')
    selected_camp_status = request.GET.get('camp_status', 'ALL')
    selected_dept = request.GET.get('department', 'ALL')
    search_q = request.GET.get('q', '').strip()

    workers = Employee.objects.all().select_related('assigned_mess')

    if selected_status and selected_status != 'ALL':
        workers = workers.filter(status=selected_status)
    if selected_camp_status and selected_camp_status != 'ALL':
        workers = workers.filter(camp_status=selected_camp_status)
    if selected_dept and selected_dept != 'ALL':
        workers = workers.filter(department__icontains=selected_dept)
    if search_q:
        workers = workers.filter(
            Q(name__icontains=search_q) |
            Q(emp_id__icontains=search_q) |
            Q(camp_room__icontains=search_q) |
            Q(contact_info__icontains=search_q)
        )

    workers = workers.order_by('department', 'name')[:1500]

    rows_html = []
    for emp in workers:
        emp_code = f"EMP-{emp.id:04d}" if '@' in (emp.emp_id or '') else (emp.emp_id or f"EMP-{emp.id:04d}")
        camp_badge = '<span style="color:#16a34a; font-weight:bold;">🟢 Inside</span>' if emp.camp_status == 'INSIDE' else '<span style="color:#dc2626; font-weight:bold;">🔴 Outside</span>'
        status_color = '#16a34a' if emp.status == 'Active' else ('#eab308' if emp.status == 'On Leave' else '#dc2626')

        rows_html.append(f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 6px 8px; font-weight: bold; color:#2563eb;">{emp_code}</td>
            <td style="padding: 6px 8px; font-weight: bold;">{emp.name}<br><small style="color:#64748b; font-weight:normal;">{emp.designation or 'Staff'}</small></td>
            <td style="padding: 6px 8px;">{emp.department or 'General'}</td>
            <td style="padding: 6px 8px; font-weight:bold; color:#0284c7;">{emp.camp_room or 'Unassigned'}</td>
            <td style="padding: 6px 8px;">{camp_badge}</td>
            <td style="padding: 6px 8px; font-weight:bold; color:{status_color};">{emp.status}</td>
            <td style="padding: 6px 8px;">{emp.contact_info or '-'}</td>
            <td style="padding: 6px 8px; color:#64748b;">{emp.joining_date.strftime('%d %b %Y') if emp.joining_date else '-'}</td>
        </tr>
        """)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Camp Workforce Roster Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; color: #1e293b; }}
            h2 {{ color: #0f172a; border-bottom: 2px solid #8b5cf6; padding-bottom: 8px; margin-bottom: 4px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 11px; }}
            th {{ background: #1e293b; color: white; padding: 8px; text-align: left; font-size: 11px; }}
            @media print {{ @page {{ size: landscape; margin: 10mm; }} }}
        </style>
    </head>
    <body>
        <h2>👥 Camp HR Workforce Roster &amp; Room Allocation</h2>
        <p style="font-size: 12px; color: #64748b; margin: 0 0 12px 0;">Generated on: {timezone.now().strftime('%d %b %Y, %H:%M:%S')} | Total Listed Workers: {workers.count()}</p>
        <table>
            <thead>
                <tr>
                    <th>Emp ID</th>
                    <th>Worker Name</th>
                    <th>Department</th>
                    <th>Camp Room</th>
                    <th>Camp Presence</th>
                    <th>Status</th>
                    <th>Phone / Contact</th>
                    <th>Joining Date</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows_html)}
            </tbody>
        </table>
        <script>window.onload = function() {{ window.print(); }}</script>
    </body>
    </html>
    """
    return HttpResponse(html)


@login_required
def api_render_native_pdf(request):
    """
    Renders any report URL into an authentic, native vector PDF file (%PDF-1.4)
    with genuine selectable text and sharp vector tables, bypassing rasterization.
    """
    import os
    import re
    import shutil
    import tempfile
    import subprocess
    import datetime
    from django.test import Client

    target_url = request.GET.get('url')
    title = request.GET.get('title') or 'Project_Report'
    if not target_url:
        return HttpResponse('Missing url parameter', status=400)

    client = Client()
    if request.user.is_authenticated:
        client.force_login(request.user)

    resp = client.get(target_url)
    if resp.status_code != 200:
        return HttpResponse(f'Target returned status {resp.status_code}', status=resp.status_code)

    ct = resp.get('Content-Type', '')
    if 'application/pdf' in ct:
        return HttpResponse(resp.content, content_type='application/pdf')

    html = resp.content.decode('utf-8', errors='ignore')
    # Clean no-print interactive elements and auto-print scripts
    html_clean = re.sub(r'<div class="no-print"[\s\S]*?</div>', '', html)
    html_clean = re.sub(r'<script[\s\S]*?</script>', '', html_clean)

    # Detect headless Chromium / Edge on system
    browser = None
    candidates = [
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    ]
    for c in candidates:
        if os.path.exists(c):
            browser = c
            break
    if not browser:
        for name in ['google-chrome', 'chromium', 'chromium-browser', 'msedge', 'chrome']:
            found = shutil.which(name)
            if found:
                browser = found
                break

    if browser:
        fd, temp_html = tempfile.mkstemp(suffix='.html')
        os.close(fd)
        with open(temp_html, 'w', encoding='utf-8') as f:
            f.write(html_clean)

        temp_pdf = temp_html.replace('.html', '.pdf')
        try:
            cmd = [
                browser,
                '--headless=new',
                '--disable-gpu',
                '--no-sandbox',
                '--run-all-compositor-stages-before-draw',
                f'--print-to-pdf={temp_pdf}',
                '--print-to-pdf-no-header',
                temp_html
            ]
            subprocess.run(cmd, check=True, timeout=30)
            with open(temp_pdf, 'rb') as f:
                pdf_bytes = f.read()

            safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', title)
            filename = f"{safe_title}_{datetime.date.today().isoformat()}.pdf"
            response = HttpResponse(pdf_bytes, content_type='application/pdf')
            response['Content-Disposition'] = f'inline; filename="{filename}"'
            return response
        except Exception as e:
            pass
        finally:
            if os.path.exists(temp_html):
                try:
                    os.remove(temp_html)
                except Exception:
                    pass
            if os.path.exists(temp_pdf):
                try:
                    os.remove(temp_pdf)
                except Exception:
                    pass

    return HttpResponse(resp.content, content_type=ct)


# ==============================================================================
# LEAVE MANAGEMENT & AUDIT TRAIL APIS
# ==============================================================================

@login_required
def api_leave_create(request):
    """
    Save official Leave Application (Labour Multi-MSW or Company Staff) to DB.
    Generates sequential official Form No (e.g., MSW-2026-0001 or LV-2026-0001).
    Updates Employee status to 'On Leave' / camp_status='ON_LEAVE', logs CampMovementLog OUT,
    and returns form_number and created records.
    """
    from django.http import JsonResponse
    from django.utils import timezone
    from datetime import datetime
    from portal.models import Employee, EmployeeLeaveRecord, CampMovementLog
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': f'Invalid JSON: {str(e)}'}, status=400)

    leave_type = data.get('leave_type', 'LABOUR_BATCH')  # 'LABOUR_BATCH' or 'COMPANY_STAFF'
    cur_year = timezone.now().year

    # Generate sequential Form Number
    prefix = 'MSW' if leave_type == 'LABOUR_BATCH' else 'LV'
    existing_records = EmployeeLeaveRecord.objects.filter(
        form_number__startswith=f"{prefix}-{cur_year}-"
    )
    highest_num = 0
    for rec in existing_records:
        parts = (rec.form_number or '').split('-')
        if len(parts) >= 3 and parts[-1].isdigit():
            highest_num = max(highest_num, int(parts[-1]))
    form_number = f"{prefix}-{cur_year}-{highest_num + 1:04d}"

    def parse_dt(s):
        if not s:
            return timezone.now().date()
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(str(s).strip(), fmt).date()
            except ValueError:
                continue
        return timezone.now().date()

    raw_start = data.get('start_date')
    raw_end = data.get('end_date')
    start_date = parse_dt(raw_start)
    end_date = parse_dt(raw_end)
    total_days = int(data.get('total_days') or max(1, (end_date - start_date).days + 1))
    leave_category = data.get('leave_category') or ('Casual Leave' if leave_type == 'COMPANY_STAFF' else 'Home Leave')
    reason = (data.get('reason') or '').strip()
    destination = (data.get('destination') or '').strip()
    contact_number = (data.get('contact_number') or '').strip()
    replacement_worker = (data.get('replacement_worker') or '').strip()
    approved_by = (data.get('approved_by') or 'Manager Sir / HR').strip()
    remarks = (data.get('remarks') or '').strip()

    workers_data = data.get('workers', [])
    if not workers_data and (data.get('emp_id') or data.get('id')):
        workers_data = [{
            'emp_id': data.get('emp_id') or data.get('id'),
            'name': data.get('name', ''),
            'designation': data.get('designation', ''),
            'contact': contact_number,
            'destination': destination,
            'days': total_days,
            'start_date': raw_start,
            'end_date': raw_end,
        }]

    if not workers_data:
        return JsonResponse({'status': 'error', 'msg': 'No workers specified for leave'}, status=400)

    created_records = []
    for w in workers_data:
        eid = w.get('emp_id') or w.get('id')
        emp = None
        if eid:
            if str(eid).isdigit():
                emp = Employee.objects.filter(id=int(eid)).first()
            if not emp:
                emp = Employee.objects.filter(emp_id=str(eid).strip()).first()
            if not emp:
                emp = Employee.objects.filter(name__iexact=str(eid).strip()).first()

        if not emp:
            w_name = str(w.get('name', '')).strip()
            if w_name:
                emp = Employee.objects.filter(name__icontains=w_name).first()

        if not emp:
            continue

        w_start = parse_dt(w.get('start_date')) if w.get('start_date') else start_date
        w_end = parse_dt(w.get('end_date')) if w.get('end_date') else end_date
        w_days = int(w.get('days') or total_days)
        w_dest = (w.get('destination') or destination).strip()
        w_contact = (w.get('contact') or contact_number or emp.contact_info or '').strip()

        rec = EmployeeLeaveRecord.objects.create(
            form_number=form_number,
            leave_type=leave_type,
            employee=emp,
            start_date=w_start,
            end_date=w_end,
            total_days=w_days,
            leave_category=leave_category,
            reason=reason,
            destination=w_dest,
            contact_number=w_contact,
            replacement_worker=replacement_worker,
            status='ACTIVE_ON_LEAVE',
            approved_by=approved_by,
            remarks=remarks,
            created_by=request.user if request.user.is_authenticated else None,
        )
        created_records.append(rec)

        # Update Employee status
        emp.status = 'On Leave'
        emp.camp_status = 'ON_LEAVE'
        emp.shift_remarks = f"[{leave_category}] Leave ({form_number}): {w_start.strftime('%d/%m/%Y')} to {w_end.strftime('%d/%m/%Y')} - {reason or 'Approved Leave'}"
        emp.save(update_fields=['status', 'camp_status', 'shift_remarks'])

        # Gate Log Outward Movement
        try:
            CampMovementLog.objects.create(
                employee=emp,
                direction='OUT',
                gate_name='Main Camp Gate',
                purpose='LEAVE',
                remarks=f"Leave Departure: Form #{form_number} ({leave_category}) to {w_dest}"
            )
        except Exception:
            pass

    return JsonResponse({
        'status': 'SUCCESS',
        'form_number': form_number,
        'count': len(created_records),
        'message': f"Official leave registered under Form #{form_number} for {len(created_records)} worker(s)."
    })


@login_required
def api_leave_return(request):
    """
    Mark worker returned from leave.
    Updates EmployeeLeaveRecord to 'RETURNED' with actual_return_date = today.
    Updates Employee status to 'Active', camp_status='INSIDE'.
    Logs Gate Inward Movement.
    """
    from django.http import JsonResponse
    from django.utils import timezone
    from portal.models import Employee, EmployeeLeaveRecord, CampMovementLog
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    record_id = data.get('record_id')
    emp_id = data.get('emp_id')
    today = timezone.now().date()

    record = None
    if record_id:
        record = EmployeeLeaveRecord.objects.filter(id=record_id).select_related('employee').first()

    emp = None
    if record:
        emp = record.employee
    elif emp_id:
        if str(emp_id).isdigit():
            emp = Employee.objects.filter(id=int(emp_id)).first()
        if not emp:
            emp = Employee.objects.filter(emp_id=str(emp_id).strip()).first()
        if emp:
            record = EmployeeLeaveRecord.objects.filter(employee=emp, status='ACTIVE_ON_LEAVE').order_by('-created_at').first()

    if not emp:
        return JsonResponse({'status': 'error', 'msg': 'Worker not found'}, status=404)

    if record:
        record.status = 'RETURNED'
        record.actual_return_date = today
        if data.get('remarks'):
            record.remarks = f"{record.remarks or ''} | Returned: {data.get('remarks')}".strip(' |')
        record.save(update_fields=['status', 'actual_return_date', 'remarks'])

    # Re-activate employee
    emp.status = 'Active'
    emp.camp_status = 'INSIDE'
    emp.shift_remarks = f"Duty Resumed on {today.strftime('%d/%m/%Y')} (Leave Form #{record.form_number if record else 'N/A'})"
    emp.save(update_fields=['status', 'camp_status', 'shift_remarks'])

    # Log Gate Inward Movement
    try:
        CampMovementLog.objects.create(
            employee=emp,
            direction='IN',
            gate_name='Main Camp Gate',
            purpose='RETURN_DUTY',
            remarks=f"Resumed Duty: From Leave Form #{record.form_number if record else 'N/A'}"
        )
    except Exception:
        pass

    return JsonResponse({
        'status': 'SUCCESS',
        'message': f"{emp.name} ({emp.emp_id or 'ID'}) has officially resumed duty. Biometric camp status is now INSIDE.",
        'form_number': record.form_number if record else '',
        'emp_id': emp.id,
    })


@login_required
def api_leave_records(request):
    """
    API endpoint returning all leave records with filters for the Leave Register & History Hub.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from portal.models import EmployeeLeaveRecord

    status_filter = request.GET.get('status', 'ALL').strip()
    type_filter = request.GET.get('type', 'ALL').strip()
    search = request.GET.get('q', '').strip()

    qs = EmployeeLeaveRecord.objects.select_related('employee').all()

    if status_filter and status_filter != 'ALL':
        qs = qs.filter(status=status_filter)
    if type_filter and type_filter != 'ALL':
        qs = qs.filter(leave_type=type_filter)
    if search:
        qs = qs.filter(
            Q(form_number__icontains=search) |
            Q(employee__name__icontains=search) |
            Q(employee__emp_id__icontains=search) |
            Q(destination__icontains=search) |
            Q(reason__icontains=search) |
            Q(employee__contractor_agency__icontains=search)
        )

    records = []
    for r in qs.order_by('-created_at')[:250]:
        e = r.employee
        records.append({
            'id': r.id,
            'form_number': r.form_number or 'LV-N/A',
            'leave_type': r.leave_type,
            'leave_type_display': 'Labour Multi-MSW' if r.leave_type == 'LABOUR_BATCH' else 'Company Staff',
            'emp_id': e.emp_id or '',
            'emp_name': e.name or '',
            'designation': e.designation or '',
            'agency': e.contractor_agency or 'Company',
            'camp_room': e.camp_room or 'N/A',
            'start_date': r.start_date.strftime('%d/%m/%Y'),
            'end_date': r.end_date.strftime('%d/%m/%Y'),
            'total_days': r.total_days,
            'leave_category': r.leave_category or 'General Leave',
            'reason': r.reason or '',
            'destination': r.destination or '',
            'contact_number': r.contact_number or e.contact_info or '',
            'replacement_worker': r.replacement_worker or '',
            'status': r.status,
            'status_display': r.get_status_display(),
            'actual_return_date': r.actual_return_date.strftime('%d/%m/%Y') if r.actual_return_date else '',
            'approved_by': r.approved_by or '',
            'created_at': r.created_at.strftime('%d/%m/%Y %I:%M %p') if r.created_at else '',
        })

    return JsonResponse({'status': 'SUCCESS', 'records': records, 'total_count': len(records)})


# ==============================================================================
# MESS & OPERATIONAL ASSET MANAGEMENT APIS
# ==============================================================================

@login_required
def api_mess_asset_list(request):
    """
    Returns full list of Mess Asset Items with inventory numbers and status.
    """
    from django.http import JsonResponse
    from portal.models import MessAssetItem

    category = request.GET.get('category', 'ALL').strip()
    search = request.GET.get('q', '').strip()

    qs = MessAssetItem.objects.select_related('mess_location').all()
    if category and category != 'ALL':
        qs = qs.filter(category=category)
    if search:
        qs = qs.filter(name__icontains=search)

    items = []
    for it in qs.order_by('name'):
        items.append({
            'id': it.id,
            'name': it.name,
            'category': it.category,
            'category_display': it.get_category_display(),
            'asset_code': it.asset_code or '',
            'mess_location_id': it.mess_location.id if it.mess_location else None,
            'mess_location_name': it.mess_location.name if it.mess_location else 'All Messes / General',
            'total_quantity': it.total_quantity,
            'available_quantity': it.available_quantity,
            'issued_quantity': max(0, it.total_quantity - it.available_quantity),
            'unit': it.unit,
            'condition_status': it.condition_status,
            'condition_display': it.get_condition_status_display(),
            'notes': it.notes or '',
        })

    return JsonResponse({'status': 'SUCCESS', 'assets': items})


@login_required
def api_mess_asset_create(request):
    """
    Add new Mess Asset item to inventory.
    """
    from django.http import JsonResponse
    from portal.models import MessAssetItem, MessLocation
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': f'Invalid JSON: {str(e)}'}, status=400)

    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'msg': 'Asset name is required'}, status=400)

    category = data.get('category', 'DINING_UTENSILS')
    asset_code = (data.get('asset_code') or '').strip() or None
    total_qty = int(data.get('total_quantity') or 1)
    unit = (data.get('unit') or 'Pcs').strip()
    condition = data.get('condition_status') or data.get('condition') or 'GOOD'
    mess_loc_id = data.get('mess_location_id')
    notes = (data.get('notes') or '').strip()

    mess_loc = None
    if mess_loc_id:
        mess_loc = MessLocation.objects.filter(id=mess_loc_id).first()

    # Generate asset_code if missing, ensuring uniqueness
    if not asset_code:
        prefix = 'MESS-' + (category[:3].upper() if category else 'AST')
        cnt = MessAssetItem.objects.count() + 1
        asset_code = f"{prefix}-{cnt:03d}"
        while MessAssetItem.objects.filter(asset_code=asset_code).exists():
            cnt += 1
            asset_code = f"{prefix}-{cnt:03d}"
    else:
        if MessAssetItem.objects.filter(asset_code=asset_code).exists():
            return JsonResponse({
                'status': 'error',
                'msg': f"Asset Code '{asset_code}' already exists! Please leave it blank to auto-generate or use a unique code."
            }, status=400)

    try:
        item = MessAssetItem.objects.create(
            name=name,
            category=category,
            asset_code=asset_code,
            mess_location=mess_loc,
            total_quantity=total_qty,
            available_quantity=total_qty,
            unit=unit,
            condition_status=condition,
            notes=notes,
        )
    except Exception as ex:
        return JsonResponse({'status': 'error', 'msg': f"Failed to save asset: {str(ex)}"}, status=400)

    return JsonResponse({
        'status': 'SUCCESS',
        'message': f"Asset '{item.name}' added successfully to inventory.",
        'item': {
            'id': item.id,
            'name': item.name,
            'asset_code': item.asset_code,
            'total_quantity': item.total_quantity,
            'unit': item.unit,
        }
    })


@login_required
def api_mess_asset_issue(request):
    """
    Issue an asset to a staff member (Cook, Cleaner, Helper, Incharge) or to a Mess Location.
    Decrements asset.available_quantity.
    """
    from django.http import JsonResponse
    from django.utils import timezone
    from portal.models import MessAssetItem, MessAssetAllocation, Employee, MessLocation
    from datetime import datetime
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': f'Invalid JSON: {str(e)}'}, status=400)

    asset_id = data.get('asset_id')
    asset = MessAssetItem.objects.filter(id=asset_id).first()
    if not asset:
        return JsonResponse({'status': 'error', 'msg': 'Asset item not found'}, status=404)

    qty = int(data.get('quantity') or 1)
    if qty <= 0:
        return JsonResponse({'status': 'error', 'msg': 'Quantity must be greater than 0'}, status=400)

    if asset.available_quantity < qty:
        return JsonResponse({
            'status': 'error',
            'msg': f'Insufficient available stock! Available: {asset.available_quantity} {asset.unit}, Requested: {qty} {asset.unit}'
        }, status=400)

    alloc_type = data.get('allocated_to_type', 'MESS')
    location_name = (data.get('location_name') or '').strip()
    staff_name = (data.get('staff_name') or '').strip()
    staff_role = (data.get('staff_role') or '').strip()
    vehicle_number = (data.get('vehicle_number') or '').strip()
    handover_to = (data.get('handover_to') or '').strip()
    handover_phone = (data.get('handover_phone') or '').strip()
    gate_pass_no = (data.get('gate_pass_no') or '').strip()

    staff_member = None
    staff_id = data.get('staff_member_id')
    if staff_id:
        if str(staff_id).isdigit():
            staff_member = Employee.objects.filter(id=int(staff_id)).first()
        if not staff_member:
            staff_member = Employee.objects.filter(emp_id=str(staff_id).strip()).first()
        if not staff_member:
            staff_member = Employee.objects.filter(name__iexact=str(staff_id).strip()).first()

    if staff_member and not staff_name:
        staff_name = staff_member.name
    if not staff_role and staff_member:
        staff_role = staff_member.designation or 'Kitchen Staff'

    mess_loc = None
    mess_loc_id = data.get('mess_location_id')
    if mess_loc_id:
        mess_loc = MessLocation.objects.filter(id=mess_loc_id).first()
        if mess_loc and not location_name:
            location_name = mess_loc.name

    def parse_dt(s):
        if not s:
            return None
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(str(s).strip(), fmt).date()
            except ValueError:
                continue
        return None

    issue_date = parse_dt(data.get('issue_date')) or timezone.now().date()
    exp_return = parse_dt(data.get('expected_return_date'))
    cond = (data.get('condition_on_issue') or 'Good Condition').strip()
    remarks = (data.get('remarks') or '').strip()

    from portal.models import CampRoom
    room_obj = None
    room_str = (data.get('room_number') or '').strip()
    room_id = data.get('room_id')
    if room_id and str(room_id).isdigit():
        room_obj = CampRoom.objects.filter(id=int(room_id)).first()
        if room_obj:
            room_str = room_obj.room_number
    elif room_str:
        room_obj = CampRoom.objects.filter(room_number__iexact=room_str).first()

    target_name = ''
    if alloc_type == 'ROOM':
        if not room_str and room_obj:
            room_str = room_obj.room_number
        target_name = f"Room {room_str or 'N/A'}"
        staff_role = 'Room Asset'
    elif alloc_type == 'RESIDENT':
        if not room_str and staff_member and staff_member.camp_room:
            room_str = staff_member.camp_room
            if not room_obj:
                room_obj = CampRoom.objects.filter(room_number__iexact=room_str).first()
        staff_role = staff_role or 'Room Resident'
        target_name = f"{staff_member.name if staff_member else (staff_name or 'Resident')} (Room {room_str or 'N/A'})"
    elif alloc_type == 'OTHER_SITE':
        staff_role = staff_role or 'Site Transfer'
        dest = location_name or 'Other Site'
        parts = [f"🚚 {dest}"]
        if handover_to:
            parts.append(f"(Handover: {handover_to})")
        if vehicle_number:
            parts.append(f"[Veh: {vehicle_number}]")
        target_name = " ".join(parts)
        if not staff_name:
            staff_name = handover_to
    else: # MESS, MESS_LOCATION, STAFF_MEMBER
        alloc_type = 'MESS'
        parts = []
        if staff_name:
            role_text = f" ({staff_role})" if staff_role else ""
            parts.append(f"{staff_name}{role_text}")
        elif staff_member:
            role_text = f" ({staff_role})" if staff_role else ""
            parts.append(f"{staff_member.name}{role_text}")
        
        if location_name:
            parts.append(f"@{location_name}")
        elif mess_loc:
            parts.append(f"@{mess_loc.name}")
        
        target_name = " ".join(parts) if parts else (staff_name or location_name or 'Mess Facility')
        if not staff_role:
            staff_role = 'Kitchen Staff' if staff_name else 'Mess Facility'

    alloc = MessAssetAllocation.objects.create(
        asset=asset,
        allocated_to_type=alloc_type,
        room=room_obj,
        room_number=room_str,
        staff_member=staff_member,
        staff_name=staff_name or (staff_member.name if staff_member else None),
        staff_role=staff_role,
        mess_location=mess_loc,
        location_name=location_name or (mess_loc.name if mess_loc else None),
        vehicle_number=vehicle_number or None,
        handover_to=handover_to or None,
        handover_phone=handover_phone or None,
        gate_pass_no=gate_pass_no or None,
        quantity=qty,
        issue_date=issue_date,
        expected_return_date=exp_return,
        status='ISSUED',
        condition_on_issue=cond,
        issued_by=request.user if request.user.is_authenticated else None,
        remarks=remarks,
    )

    # Decrement available quantity
    asset.available_quantity = max(0, asset.available_quantity - qty)
    asset.save(update_fields=['available_quantity'])

    # Two-way sync with Room Management
    try:
        from portal.models import CampAssetAllotment
        asset_name_lower = (asset.name or '').lower()
        asset_cat_lower = (asset.category or '').lower()

        if alloc_type == 'ROOM' and room_obj:
            if 'fan' in asset_name_lower or 'fan' in asset_cat_lower:
                room_obj.fan_count = (room_obj.fan_count or 0) + qty
                room_obj.fan_status = 'WORKING'
                room_obj.save(update_fields=['fan_count', 'fan_status'])
            elif any(k in asset_name_lower for k in ['tube', 'light', 'bulb', 'led']):
                room_obj.tubelight_count = (room_obj.tubelight_count or 0) + qty
                room_obj.tubelight_status = 'WORKING'
                room_obj.save(update_fields=['tubelight_count', 'tubelight_status'])

        elif alloc_type == 'RESIDENT' and staff_member:
            allot, _ = CampAssetAllotment.objects.get_or_create(employee=staff_member)
            if room_obj and not allot.room:
                allot.room = room_obj
            if not staff_member.camp_room and room_str:
                staff_member.camp_room = room_str
                staff_member.camp_status = 'INSIDE'
                staff_member.save(update_fields=['camp_room', 'camp_status'])

            if any(k in asset_name_lower for k in ['bed', 'cot']):
                allot.bed_cot_allotted = True
            if 'mattress' in asset_name_lower:
                allot.mattress_allotted = True
            if 'pillow' in asset_name_lower:
                allot.pillow_allotted = True
            if 'blanket' in asset_name_lower:
                allot.blanket_allotted = True
            if 'fan' in asset_name_lower:
                allot.fan_allotted = True
            if any(k in asset_name_lower for k in ['bucket', 'mug']):
                allot.bucket_mug_issued = True
            allot.save()
    except Exception:
        pass

    return JsonResponse({
        'status': 'SUCCESS',
        'message': f"Successfully allotted {qty} {asset.unit} of '{asset.name}' to {target_name}.",
        'allocation_id': alloc.id,
        'remaining_stock': asset.available_quantity,
        'target_name': target_name,
        'allocated_to_type': alloc_type,
    })


@login_required
def api_mess_asset_return(request):
    """
    Log asset return. Updates allocation status and restores available stock.
    """
    from django.http import JsonResponse
    from django.utils import timezone
    from portal.models import MessAssetAllocation, MessAssetReturnLog
    from datetime import datetime
    import json

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': f'Invalid JSON: {str(e)}'}, status=400)

    alloc_id = data.get('allocation_id')
    alloc = MessAssetAllocation.objects.filter(id=alloc_id).select_related('asset', 'staff_member').first()
    if not alloc:
        return JsonResponse({'status': 'error', 'msg': 'Allocation record not found'}, status=404)

    qty = int(data.get('returned_quantity') or alloc.quantity)
    if qty <= 0:
        return JsonResponse({'status': 'error', 'msg': 'Returned quantity must be > 0'}, status=400)

    condition = data.get('condition', 'GOOD')  # 'GOOD', 'DAMAGED_REPAIRABLE', 'SCRAPPED', 'LOST'
    remarks = (data.get('remarks') or '').strip()

    def parse_dt(s):
        if not s:
            return timezone.now().date()
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(str(s).strip(), fmt).date()
            except ValueError:
                continue
        return timezone.now().date()

    return_date = parse_dt(data.get('return_date'))

    log = MessAssetReturnLog.objects.create(
        allocation=alloc,
        return_date=return_date,
        returned_quantity=qty,
        condition=condition,
        received_by=request.user if request.user.is_authenticated else None,
        remarks=remarks,
    )

    # Calculate previously returned total for this allocation
    prev_returned = sum(r.returned_quantity for r in alloc.return_logs.all())
    if prev_returned >= alloc.quantity:
        alloc.status = 'RETURNED'
    else:
        alloc.status = 'PARTIALLY_RETURNED'
    alloc.save(update_fields=['status'])

    # Re-stock asset if condition is good or repairable
    asset = alloc.asset
    if condition in ['GOOD', 'DAMAGED_REPAIRABLE']:
        asset.available_quantity = min(asset.total_quantity, asset.available_quantity + qty)
        asset.save(update_fields=['available_quantity'])

    # Two-way sync with Room Management on return
    try:
        from portal.models import CampAssetAllotment
        asset_name_lower = (alloc.asset.name or '').lower()
        if alloc.allocated_to_type == 'ROOM' and alloc.room:
            if 'fan' in asset_name_lower:
                alloc.room.fan_count = max(0, (alloc.room.fan_count or 0) - qty)
                alloc.room.save(update_fields=['fan_count'])
            elif any(k in asset_name_lower for k in ['tube', 'light', 'bulb', 'led']):
                alloc.room.tubelight_count = max(0, (alloc.room.tubelight_count or 0) - qty)
                alloc.room.save(update_fields=['tubelight_count'])
        elif alloc.allocated_to_type == 'RESIDENT' and alloc.staff_member:
            active_left = MessAssetAllocation.objects.filter(
                staff_member=alloc.staff_member,
                asset=alloc.asset,
                status='ISSUED'
            ).exclude(id=alloc.id).exists()
            if not active_left:
                allot = CampAssetAllotment.objects.filter(employee=alloc.staff_member).first()
                if allot:
                    if any(k in asset_name_lower for k in ['bed', 'cot']):
                        allot.bed_cot_allotted = False
                    if 'mattress' in asset_name_lower:
                        allot.mattress_allotted = False
                    if 'pillow' in asset_name_lower:
                        allot.pillow_allotted = False
                    if 'blanket' in asset_name_lower:
                        allot.blanket_allotted = False
                    if 'fan' in asset_name_lower:
                        allot.fan_allotted = False
                    if any(k in asset_name_lower for k in ['bucket', 'mug']):
                        allot.bucket_mug_issued = False
                    allot.save()
    except Exception:
        pass

    return JsonResponse({
        'status': 'SUCCESS',
        'message': f"Returned {qty} {asset.unit} of '{asset.name}' (Condition: {log.get_condition_display()}).",
        'available_quantity': asset.available_quantity,
        'allocation_status': alloc.status,
    })


@login_required
def api_mess_asset_history(request):
    """
    Returns complete chronological audit ledger of Mess Asset issuances and returns.
    """
    from django.http import JsonResponse
    from portal.models import MessAssetAllocation, MessAssetReturnLog

    allocations = MessAssetAllocation.objects.select_related('asset', 'staff_member', 'mess_location', 'issued_by').all().order_by('-created_at')[:200]
    returns = MessAssetReturnLog.objects.select_related('allocation__asset', 'allocation__staff_member', 'received_by').all().order_by('-created_at')[:200]

    events = []
    for a in allocations:
        if a.allocated_to_type == 'ROOM':
            target_str = f"🏢 Room {a.room_number or (a.room.room_number if a.room else 'N/A')}"
            role_str = "Room Fixture / Asset"
        elif a.allocated_to_type == 'RESIDENT':
            target_str = f"👤 {a.recipient_name or (a.staff_member.name if a.staff_member else 'Resident')} (Room {a.room_number or 'N/A'})"
            role_str = "Room Resident"
        elif a.allocated_to_type == 'OTHER_SITE':
            dest = a.location_name or 'Other Site'
            person = a.handover_to or a.staff_name or ''
            veh = f" [Veh: {a.vehicle_number}]" if a.vehicle_number else ''
            target_str = f"🚚 {dest} (Handover: {person}){veh}" if person else f"🚚 {dest}{veh}"
            role_str = a.staff_role or "Site Transfer"
        else: # MESS, MESS_LOCATION, STAFF_MEMBER
            loc = a.location_display
            person = a.recipient_name
            role = a.staff_role or 'Kitchen Staff'
            if person and loc:
                target_str = f"🍳 {loc} — {person} ({role})"
            elif person:
                target_str = f"👨‍🍳 {person} ({role})"
            elif loc:
                target_str = f"🍳 {loc}"
            else:
                target_str = "🍳 Mess / Kitchen"
            role_str = role

        events.append({
            'id': a.id,
            'event_type': 'ISSUED',
            'allocated_to_type': a.allocated_to_type,
            'target_type': a.allocated_to_type,
            'date': a.issue_date.strftime('%d/%m/%Y'),
            'timestamp': a.created_at.strftime('%d/%m/%Y %I:%M %p'),
            'asset_name': a.asset.name,
            'asset_code': a.asset.asset_code or '',
            'quantity': a.quantity,
            'unit': a.asset.unit,
            'target': target_str,
            'location_display': a.location_display,
            'recipient_display': a.recipient_name,
            'vehicle_number': a.vehicle_number or '',
            'handover_to': a.handover_to or '',
            'handover_phone': a.handover_phone or '',
            'gate_pass_no': a.gate_pass_no or '',
            'staff_role': role_str,
            'room_number': a.room_number or '',
            'status': a.status,
            'status_display': a.get_status_display(),
            'condition': a.condition_on_issue,
            'processed_by': a.issued_by.get_full_name() or a.issued_by.username if a.issued_by else 'System / Admin',
            'remarks': a.remarks or '',
        })

    for r in returns:
        alloc = r.allocation
        loc = alloc.location_display
        person = alloc.recipient_name
        role = alloc.staff_role or 'Kitchen Staff'
        if alloc.allocated_to_type == 'ROOM':
            target_str = f"🏢 Room {alloc.room_number or 'N/A'}"
        elif alloc.allocated_to_type == 'RESIDENT':
            target_str = f"👤 {person or 'Resident'} (Room {alloc.room_number or 'N/A'})"
        elif alloc.allocated_to_type == 'OTHER_SITE':
            dest = alloc.location_name or 'Other Site'
            veh = f" [Veh: {alloc.vehicle_number}]" if alloc.vehicle_number else ''
            target_str = f"🚚 {dest} (Handover: {person}){veh}" if person else f"🚚 {dest}{veh}"
        else:
            if person and loc:
                target_str = f"🍳 {loc} — {person} ({role})"
            elif person:
                target_str = f"👨‍🍳 {person} ({role})"
            elif loc:
                target_str = f"🍳 {loc}"
            else:
                target_str = "🍳 Mess Facility"

        events.append({
            'id': r.id,
            'event_type': 'RETURNED',
            'target_type': alloc.allocated_to_type,
            'date': r.return_date.strftime('%d/%m/%Y'),
            'timestamp': r.created_at.strftime('%d/%m/%Y %I:%M %p'),
            'asset_name': alloc.asset.name,
            'asset_code': alloc.asset.asset_code or '',
            'quantity': r.returned_quantity,
            'unit': alloc.asset.unit,
            'target': target_str,
            'location_display': loc,
            'recipient_display': person,
            'staff_role': role,
            'status': 'RETURNED',
            'status_display': f"Returned ({r.get_condition_display()})",
            'condition': r.get_condition_display(),
            'processed_by': r.received_by.get_full_name() or r.received_by.username if r.received_by else 'System / Admin',
            'remarks': r.remarks or '',
        })

    # Sort all events by date/timestamp descending
    return JsonResponse({'status': 'SUCCESS', 'events': events, 'count': len(events)})


@login_required
def api_room_occupants(request):
    """
    Returns list of resident workers living in a specific room for unified asset allotment.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from portal.models import Employee, CampRoom

    room_param = str(request.GET.get('room', '')).strip()
    if not room_param:
        return JsonResponse({'status': 'error', 'msg': 'Room parameter required'}, status=400)

    room_num = room_param
    if room_param.isdigit():
        r_obj = CampRoom.objects.filter(id=int(room_param)).first()
        if r_obj:
            room_num = r_obj.room_number

    residents_qs = Employee.objects.filter(
        status='Active'
    ).filter(
        Q(camp_room__iexact=room_num) |
        Q(allotted_camp_assets__room__room_number__iexact=room_num)
    ).distinct().order_by('name')

    residents = []
    for emp in residents_qs:
        residents.append({
            'id': emp.id,
            'name': emp.name,
            'emp_id': emp.emp_id or 'N/A',
            'designation': emp.designation or 'Worker',
            'agency': emp.contractor_agency or 'Company',
            'room': emp.camp_room or room_num,
        })

    return JsonResponse({
        'status': 'SUCCESS',
        'room_number': room_num,
        'count': len(residents),
        'residents': residents
    })


# =========================================================================
# UNIVERSAL EDIT & DELETE OPERATIONS FOR CAMP PORTAL MODULES
# =========================================================================

@csrf_exempt
@login_required
def api_camp_curfew_resolve(request, emp_id):
    """
    Resolve curfew / overstay breach for a worker by marking them INSIDE.
    """
    if not _is_camp_manager(request.user):
        return JsonResponse({'status': 'FORBIDDEN', 'msg': 'Permission Denied: Only Managers can resolve curfew breaches.'}, status=403)

    emp = get_object_or_404(Employee, id=emp_id)
    emp.camp_status = 'INSIDE'
    emp.save(update_fields=['camp_status'])

    # Create an authentic Gate IN punch
    CampMovementLog.objects.create(
        employee=emp,
        direction='IN',
        gate_name='Main Camp Gate',
        purpose='RETURN_DUTY',
        remarks=f"Curfew / overstay breach resolved by {request.user.username}"
    )

    log_activity(request.user, 'UPDATE', 'Camp Curfew', f"Manager resolved curfew breach for {emp.name} (marked INSIDE)", request)
    return JsonResponse({'status': 'SUCCESS', 'msg': f"Worker {emp.name} marked returned (INSIDE) and curfew breach resolved successfully."})


@csrf_exempt
@login_required
def api_mess_asset_edit(request, asset_id):
    """
    GET: Return asset details for edit modal.
    POST: Update asset item details and adjust quantities safely.
    """
    from portal.models import MessAssetItem
    item = get_object_or_404(MessAssetItem, id=asset_id)
    if request.method == 'GET':
        return JsonResponse({
            'status': 'SUCCESS',
            'asset': {
                'id': item.id,
                'name': item.name,
                'asset_code': item.asset_code or '',
                'category': item.category,
                'total_quantity': item.total_quantity,
                'available_quantity': item.available_quantity,
                'issued_quantity': item.issued_quantity,
                'unit': item.unit,
                'condition_status': item.condition_status,
                'notes': item.notes or '',
            }
        })
    if request.method == 'POST':
        try:
            data = json.loads(request.body) if request.body and ('application/json' in request.content_type or request.headers.get('Content-Type') == 'application/json') else request.POST
            name = (data.get('name') or '').strip()
            if not name:
                return JsonResponse({'status': 'error', 'msg': 'Asset name is required.'}, status=400)

            category = data.get('category', item.category)
            unit = (data.get('unit') or item.unit).strip()
            condition = data.get('condition_status') or item.condition_status
            notes = (data.get('notes') or '').strip()

            new_total = int(data.get('total_quantity') or item.total_quantity)
            if new_total < 0:
                return JsonResponse({'status': 'error', 'msg': 'Total quantity cannot be negative.'}, status=400)

            currently_issued = item.issued_quantity
            if new_total < currently_issued:
                return JsonResponse({
                    'status': 'error',
                    'msg': f"Cannot set total quantity to {new_total} because {currently_issued} {item.unit} are currently issued and in use!"
                }, status=400)

            item.name = name
            item.category = category
            item.unit = unit
            item.condition_status = condition
            item.notes = notes
            item.total_quantity = new_total
            item.available_quantity = new_total - currently_issued
            item.save()

            log_activity(request.user, 'UPDATE', 'Camp Asset', f"Manager edited asset '{item.name}' (Total: {item.total_quantity} {item.unit})", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"Asset '{item.name}' updated successfully."})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)
    return JsonResponse({'status': 'error', 'msg': 'Method not allowed'}, status=405)


@csrf_exempt
@login_required
def api_mess_asset_delete(request, asset_id):
    """
    Delete asset item from inventory with safeguard against deleting assets with active allotments.
    """
    from portal.models import MessAssetItem, MessAssetAllocation
    item = get_object_or_404(MessAssetItem, id=asset_id)
    item_name = item.name

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    active_count = MessAssetAllocation.objects.filter(asset=item, status__in=['ISSUED', 'PARTIALLY_RETURNED']).count()
    if active_count > 0:
        return JsonResponse({
            'status': 'error',
            'msg': f"Cannot delete '{item_name}': There are {active_count} active allocation(s) currently in use. Please return all issued items first."
        }, status=400)

    try:
        allocs = MessAssetAllocation.objects.filter(asset=item)
        for a in allocs:
            a.return_logs.all().delete()
        allocs.delete()
        item.delete()

        log_activity(request.user, 'DELETE', 'Camp Asset', f"Manager deleted inventory asset '{item_name}'", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': f"Asset '{item_name}' deleted successfully."})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


@csrf_exempt
@login_required
def api_mess_allocation_edit(request, alloc_id):
    """
    GET: Return allocation details for edit modal.
    POST: Update allocation quantity, condition, date, remarks and safely rebalance asset stock.
    """
    from portal.models import MessAssetAllocation
    alloc = get_object_or_404(MessAssetAllocation.objects.select_related('asset'), id=alloc_id)

    if request.method == 'GET':
        target_display = ''
        if alloc.allocated_to_type == 'ROOM':
            target_display = f"Room {alloc.room_number or 'N/A'}"
        elif alloc.allocated_to_type == 'RESIDENT':
            target_display = f"Resident {alloc.staff_name or (alloc.staff_member.name if alloc.staff_member else 'N/A')}"
        elif alloc.allocated_to_type == 'MESS':
            target_display = f"Mess: {alloc.location_name or 'Kitchen'}"
        else:
            target_display = alloc.location_name or 'Other Site'

        return JsonResponse({
            'status': 'SUCCESS',
            'allocation': {
                'id': alloc.id,
                'asset_id': alloc.asset.id,
                'asset_name': alloc.asset.name,
                'asset_unit': alloc.asset.unit,
                'quantity': alloc.quantity,
                'condition_on_issue': alloc.condition_on_issue or 'GOOD',
                'issue_date': alloc.issue_date.strftime('%Y-%m-%d') if alloc.issue_date else '',
                'remarks': alloc.remarks or '',
                'target_display': target_display
            }
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body) if request.body and ('application/json' in request.content_type or request.headers.get('Content-Type') == 'application/json') else request.POST
            new_qty = int(data.get('quantity') or alloc.quantity)
            if new_qty <= 0:
                return JsonResponse({'status': 'error', 'msg': 'Quantity must be greater than 0.'}, status=400)

            already_returned = sum(r.returned_quantity for r in alloc.return_logs.all())
            if new_qty < already_returned:
                return JsonResponse({
                    'status': 'error',
                    'msg': f"Cannot reduce quantity below {already_returned} {alloc.asset.unit} which have already been returned."
                }, status=400)

            asset = alloc.asset
            qty_diff = new_qty - alloc.quantity
            if qty_diff > 0:
                if asset.available_quantity < qty_diff:
                    return JsonResponse({
                        'status': 'error',
                        'msg': f"Insufficient stock: only {asset.available_quantity} {asset.unit} available in storage."
                    }, status=400)
                asset.available_quantity -= qty_diff
                asset.save(update_fields=['available_quantity'])
            elif qty_diff < 0:
                asset.available_quantity += abs(qty_diff)
                asset.save(update_fields=['available_quantity'])

            alloc.quantity = new_qty
            if data.get('condition_on_issue'):
                alloc.condition_on_issue = data.get('condition_on_issue')
            if 'remarks' in data:
                alloc.remarks = data.get('remarks', '').strip()
            if data.get('issue_date'):
                try:
                    alloc.issue_date = datetime.strptime(data.get('issue_date'), '%Y-%m-%d').date()
                except Exception:
                    pass

            if already_returned >= new_qty:
                alloc.status = 'RETURNED'
            elif already_returned > 0:
                alloc.status = 'PARTIALLY_RETURNED'
            else:
                alloc.status = 'ISSUED'

            alloc.save()
            log_activity(request.user, 'UPDATE', 'Camp Asset Allocation', f"Manager edited allocation #{alloc.id} of {alloc.asset.name} (Qty: {alloc.quantity})", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': 'Allotment updated successfully.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)

    return JsonResponse({'status': 'error', 'msg': 'Method not allowed'}, status=405)


@csrf_exempt
@login_required
def api_mess_allocation_delete(request, alloc_id):
    """
    Delete an allocation record and restore any unreturned stock to available inventory.
    """
    from portal.models import MessAssetAllocation
    alloc = get_object_or_404(MessAssetAllocation.objects.select_related('asset'), id=alloc_id)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        asset = alloc.asset
        already_returned = sum(r.returned_quantity for r in alloc.return_logs.all())
        unreturned = max(0, alloc.quantity - already_returned)
        if unreturned > 0:
            asset.available_quantity = min(asset.total_quantity, asset.available_quantity + unreturned)
            asset.save(update_fields=['available_quantity'])

        alloc.return_logs.all().delete()
        alloc.delete()

        log_activity(request.user, 'DELETE', 'Camp Asset Allocation', f"Manager deleted allocation #{alloc_id} of {asset.name} (Restored {unreturned} {asset.unit} to stock)", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': f"Allotment deleted successfully and {unreturned} {asset.unit} restored to stock."})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


@csrf_exempt
@login_required
def api_mess_return_log_delete(request, log_id):
    """
    Delete an audit return log entry.
    """
    from portal.models import MessAssetReturnLog
    log_entry = get_object_or_404(MessAssetReturnLog.objects.select_related('allocation__asset'), id=log_id)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        alloc = log_entry.allocation
        qty = log_entry.returned_quantity
        asset = alloc.asset

        if log_entry.condition in ['GOOD', 'DAMAGED_REPAIRABLE']:
            asset.available_quantity = max(0, asset.available_quantity - qty)
            asset.save(update_fields=['available_quantity'])

        log_entry.delete()

        prev_returned = sum(r.returned_quantity for r in alloc.return_logs.all())
        if prev_returned >= alloc.quantity:
            alloc.status = 'RETURNED'
        elif prev_returned > 0:
            alloc.status = 'PARTIALLY_RETURNED'
        else:
            alloc.status = 'ISSUED'
        alloc.save(update_fields=['status'])

        log_activity(request.user, 'DELETE', 'Camp Asset Return Log', f"Manager deleted return ledger entry #{log_id}", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': 'Return ledger entry deleted successfully.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


@csrf_exempt
@login_required
def api_leave_record_edit(request, record_id):
    """
    GET: Return leave record details for edit modal.
    POST: Update leave record details (dates, total_days, reason, remarks, status).
    """
    from portal.models import EmployeeLeaveRecord
    rec = get_object_or_404(EmployeeLeaveRecord.objects.select_related('employee'), id=record_id)

    if request.method == 'GET':
        return JsonResponse({
            'status': 'SUCCESS',
            'record': {
                'id': rec.id,
                'form_number': rec.form_number or '',
                'worker_name': rec.employee.name,
                'emp_id': rec.employee.emp_id or '',
                'leave_type': rec.leave_type,
                'leave_category': rec.leave_category or 'Home Leave',
                'start_date': rec.start_date.strftime('%Y-%m-%d') if rec.start_date else '',
                'end_date': rec.end_date.strftime('%Y-%m-%d') if rec.end_date else '',
                'total_days': rec.total_days,
                'destination': rec.destination or '',
                'reason': rec.reason or '',
                'remarks': rec.remarks or '',
                'status': rec.status,
            }
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body) if request.body and ('application/json' in request.content_type or request.headers.get('Content-Type') == 'application/json') else request.POST
            if data.get('start_date'):
                rec.start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
            if data.get('end_date'):
                rec.end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
            if data.get('total_days'):
                rec.total_days = int(data.get('total_days'))
            elif rec.start_date and rec.end_date:
                rec.total_days = max(1, (rec.end_date - rec.start_date).days + 1)

            if data.get('leave_category'):
                rec.leave_category = data.get('leave_category')
            if 'reason' in data:
                rec.reason = data.get('reason', '').strip()
            if 'destination' in data:
                rec.destination = data.get('destination', '').strip()
            if 'remarks' in data:
                rec.remarks = data.get('remarks', '').strip()

            new_status = data.get('status')
            if new_status and new_status in ['ACTIVE_ON_LEAVE', 'RETURNED', 'CANCELLED', 'OVERDUE']:
                rec.status = new_status
                if new_status == 'RETURNED' and not rec.actual_return_date:
                    rec.actual_return_date = timezone.now().date()
                    rec.employee.status = 'Active'
                    rec.employee.camp_status = 'INSIDE'
                    rec.employee.save(update_fields=['status', 'camp_status'])

            rec.save()
            log_activity(request.user, 'UPDATE', 'Camp Leave', f"Manager updated leave record #{rec.form_number} for {rec.employee.name}", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"Leave record #{rec.form_number} updated successfully."})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)

    return JsonResponse({'status': 'error', 'msg': 'Method not allowed'}, status=405)


@csrf_exempt
@login_required
def api_leave_record_delete(request, record_id):
    """
    Delete a leave record and re-evaluate worker active/leave status.
    """
    from portal.models import EmployeeLeaveRecord
    rec = get_object_or_404(EmployeeLeaveRecord.objects.select_related('employee'), id=record_id)
    emp = rec.employee
    form_num = rec.form_number or f"#{rec.id}"

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        was_active_leave = (rec.status == 'ACTIVE_ON_LEAVE')
        rec.delete()

        if was_active_leave:
            has_other_active = EmployeeLeaveRecord.objects.filter(employee=emp, status='ACTIVE_ON_LEAVE').exists()
            if not has_other_active:
                emp.status = 'Active'
                emp.shift_remarks = 'Working'
                emp.save(update_fields=['status', 'shift_remarks'])

        log_activity(request.user, 'DELETE', 'Camp Leave', f"Manager deleted leave record {form_num} for {emp.name}", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': f"Leave record {form_num} deleted successfully."})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


@csrf_exempt
@login_required
def api_leave_cancel_active(request, emp_id):
    """
    Directly cancel active leave for an employee from the Active Leave table.
    Sets employee to Active / Working and marks leave records CANCELLED.
    """
    from portal.models import Employee, EmployeeLeaveRecord
    emp = get_object_or_404(Employee, id=emp_id)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        active_recs = EmployeeLeaveRecord.objects.filter(employee=emp, status='ACTIVE_ON_LEAVE')
        active_recs.update(status='CANCELLED', remarks='Leave cancelled by Manager')

        emp.status = 'Active'
        emp.shift_remarks = 'Working'
        emp.save(update_fields=['status', 'shift_remarks'])

        log_activity(request.user, 'CANCEL', 'Camp Leave', f"Manager cancelled active leave for {emp.name}", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': f"Leave for {emp.name} cancelled. Worker is now Active & Working."})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


@csrf_exempt
@login_required
def api_mess_log_edit(request, log_id):
    """
    GET: Return meal log details for edit modal.
    POST: Update meal log date, punch_time, meal_type, mess_location, status, remarks.
    """
    from portal.models import MessLog, MessLocation
    log = get_object_or_404(MessLog.objects.select_related('employee', 'mess_location'), id=log_id)

    if request.method == 'GET':
        emp_code = f"EMP-{log.employee.id:04d}" if '@' in (log.employee.emp_id or '') else (log.employee.emp_id or '')
        return JsonResponse({
            'status': 'SUCCESS',
            'log': {
                'id': log.id,
                'worker_name': log.employee.name,
                'emp_id': emp_code,
                'date': log.date.strftime('%Y-%m-%d') if log.date else '',
                'time': log.punch_time.strftime('%H:%M') if log.punch_time else '',
                'meal_type': log.meal_type,
                'mess_location_id': log.mess_location.id if log.mess_location else None,
                'mess_location_name': log.mess_location.name if log.mess_location else 'Main Canteen',
                'status': log.status,
                'remarks': log.remarks or '',
            }
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body) if request.body and ('application/json' in request.content_type or request.headers.get('Content-Type') == 'application/json') else request.POST
            if data.get('date'):
                try:
                    log.date = datetime.strptime(data.get('date'), '%Y-%m-%d').date()
                except Exception:
                    pass
            if data.get('date') and data.get('time'):
                try:
                    dt_combined = datetime.strptime(f"{data.get('date')} {data.get('time')}", '%Y-%m-%d %H:%M')
                    log.punch_time = timezone.make_aware(dt_combined) if timezone.is_naive(dt_combined) else dt_combined
                except Exception:
                    pass
            elif data.get('time') and log.date:
                try:
                    dt_combined = datetime.strptime(f"{log.date} {data.get('time')}", '%Y-%m-%d %H:%M')
                    log.punch_time = timezone.make_aware(dt_combined) if timezone.is_naive(dt_combined) else dt_combined
                except Exception:
                    pass

            if data.get('meal_type'):
                log.meal_type = data.get('meal_type')
            if data.get('mess_location_id'):
                loc = MessLocation.objects.filter(id=data.get('mess_location_id')).first()
                if loc:
                    log.mess_location = loc
            if data.get('status'):
                log.status = data.get('status')
            if 'remarks' in data:
                log.remarks = data.get('remarks', '').strip()

            log.save()
            log_activity(request.user, 'UPDATE', 'Mess Log', f"Manager edited meal punch #{log.id} for {log.employee.name} ({log.meal_type})", request)
            return JsonResponse({'status': 'SUCCESS', 'msg': f"Meal log for {log.employee.name} updated successfully."})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)

    return JsonResponse({'status': 'error', 'msg': 'Method not allowed'}, status=405)


@csrf_exempt
@login_required
def api_mess_log_delete(request, log_id):
    """
    Delete a meal log entry.
    """
    from portal.models import MessLog
    log = get_object_or_404(MessLog.objects.select_related('employee'), id=log_id)
    worker_name = log.employee.name
    meal_type = log.get_meal_type_display()

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        log.delete()
        log_activity(request.user, 'DELETE', 'Mess Log', f"Manager deleted meal punch #{log_id} ({meal_type}) for {worker_name}", request)
        return JsonResponse({'status': 'SUCCESS', 'msg': f"Meal punch ({meal_type}) for {worker_name} deleted successfully."})
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': str(e)}, status=400)


@login_required
def api_camp_search_workers(request):
    """
    Live Search Worker API across all 3,238 employees (Company, Contractor, Hiring, Desuup, etc.)
    Searches by name, emp_id, contractor_agency, designation, camp_room, cid_number.
    Returns list of workers with their room & bed allotment.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from portal.models import Employee, CampAssetAllotment

    q = request.GET.get('q', '').strip()
    category = request.GET.get('category', '').strip()
    limit = int(request.GET.get('limit', 25))

    qs = Employee.objects.select_related('allotted_camp_assets').exclude(name__in=['0', '', 'None', '-', 'null'])

    if q:
        qs = qs.filter(
            Q(name__icontains=q) |
            Q(emp_id__icontains=q) |
            Q(contractor_agency__icontains=q) |
            Q(designation__icontains=q) |
            Q(camp_room__icontains=q) |
            Q(cid_number__icontains=q)
        )
    else:
        qs = qs.filter(status='Active')

    if category and category != 'ALL':
        if category.upper() == 'COMPANY':
            qs = qs.filter(Q(contractor_agency__isnull=True) | Q(contractor_agency='') | Q(contractor_agency__iexact='Company'))
        elif category.upper() == 'CONTRACTOR':
            qs = qs.exclude(Q(contractor_agency__isnull=True) | Q(contractor_agency='') | Q(contractor_agency__iexact='Company') | Q(contractor_agency__icontains='Hiring'))
        elif category.upper() == 'HIRING':
            qs = qs.filter(contractor_agency__icontains='Hiring')

    workers = []
    for emp in qs.order_by('name')[:limit]:
        asset = getattr(emp, 'allotted_camp_assets', None)
        agency = emp.contractor_agency or 'Company'
        badge_type = 'Company'
        if 'Hiring' in agency or 'Aggcon' in agency:
            badge_type = 'Hiring'
        elif agency != 'Company':
            badge_type = 'Contractor'

        workers.append({
            'id': emp.id,
            'name': emp.name,
            'emp_id': emp.emp_id or '',
            'designation': emp.designation or 'Worker',
            'agency': agency,
            'contractor_agency': agency,
            'badge_type': badge_type,
            'camp_room': emp.camp_room or 'Unassigned',
            'room': emp.camp_room or '',
            'camp_bed': asset.bed_number if asset and asset.bed_number else 'Bed 1',
            'bed': asset.bed_number if asset and asset.bed_number else 'Bed 1',
            'camp_status': emp.camp_status or 'INSIDE',
            'status': emp.status or 'Active',
            'contact_info': emp.contact_info or '',
        })

    return JsonResponse({
        'status': 'SUCCESS',
        'count': len(workers),
        'workers': workers
    })


@login_required
def api_camp_quick_add_worker(request):
    """
    On-the-spot Quick Add Worker from Asset / Room Management.
    Creates Employee and CampAssetAllotment immediately so user can allot items / room right away.
    """
    from django.http import JsonResponse
    from django.utils import timezone
    from portal.models import Employee, CampRoom, CampAssetAllotment
    import json
    import random

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'status': 'error', 'msg': f'Invalid JSON: {str(e)}'}, status=400)

    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'msg': 'Worker name is required'}, status=400)

    worker_type = data.get('worker_type', 'Contractor')
    contractor_agency = (data.get('contractor_agency') or '').strip()
    if worker_type == 'Company':
        contractor_agency = 'Company'
    elif not contractor_agency:
        contractor_agency = 'Contractor'

    emp_id = (data.get('emp_id') or '').strip()
    if not emp_id:
        prefix = 'EMP' if worker_type == 'Company' else ('HIR' if worker_type == 'Hiring' else 'CON')
        for _ in range(20):
            cand = f"{prefix}-{random.randint(1000, 9999)}"
            if not Employee.objects.filter(emp_id=cand).exists():
                emp_id = cand
                break
        if not emp_id:
            emp_id = f"{prefix}-{Employee.objects.count() + 1}"

    if Employee.objects.filter(emp_id=emp_id).exists():
        return JsonResponse({'status': 'error', 'msg': f'Employee ID {emp_id} already exists in database.'}, status=400)

    designation = (data.get('designation') or 'Worker').strip()
    camp_room = (data.get('camp_room') or '').strip()
    camp_bed = (data.get('camp_bed') or 'Bed 1').strip()
    contact_info = (data.get('contact_info') or '').strip()
    nationality = (data.get('nationality') or 'Indian').strip()

    emp = Employee.objects.create(
        name=name,
        emp_id=emp_id,
        designation=designation,
        contractor_agency=contractor_agency,
        camp_room=camp_room or 'Outside',
        camp_status='INSIDE' if camp_room else 'INSIDE',
        status='Active',
        contact_info=contact_info,
        nationality=nationality,
        joining_date=timezone.now().date(),
    )

    room_obj = None
    if camp_room:
        room_obj = CampRoom.objects.filter(room_number__iexact=camp_room).first()

    allot = CampAssetAllotment.objects.create(
        employee=emp,
        room=room_obj,
        bed_number=camp_bed,
        bed_cot_allotted=True,
        mattress_allotted=True,
        pillow_allotted=True,
        blanket_allotted=True,
        fan_allotted=True,
        condition='GOOD',
        issue_date=timezone.now().date(),
    )

    return JsonResponse({
        'status': 'SUCCESS',
        'message': f"Worker '{emp.name}' ({emp.emp_id}) added successfully!",
        'worker': {
            'id': emp.id,
            'name': emp.name,
            'emp_id': emp.emp_id,
            'designation': emp.designation,
            'agency': emp.contractor_agency,
            'contractor_agency': emp.contractor_agency,
            'badge_type': worker_type,
            'camp_room': emp.camp_room,
            'room': emp.camp_room,
            'camp_bed': allot.bed_number,
            'bed': allot.bed_number,
            'camp_status': emp.camp_status,
            'status': emp.status,
            'contact_info': emp.contact_info,
        }
    })


@login_required
def api_camp_export_preview(request):
    """
    Live Interactive Data Preview & Record Selector for Camp & Mess Export Hub.
    Returns structured column headers and filterable rows with IDs for user selection checkboxes.
    Supports block-wise and room-wise granular filtering with natural numerical room sorting.
    """
    from django.http import JsonResponse
    from django.db.models import Q
    from collections import defaultdict, Counter
    from portal.models import (
        CampRoom, CampBlock, Employee, EmployeeLeaveRecord,
        CampAssetAllotment, MessAssetItem, MessAssetAllocation,
        MessLog, MessWastageLog, MessFeedback, MessMenu, MessMealWindow,
        CampMovementLog
    )

    module = request.GET.get('module', 'rooms').strip().lower()
    include_block_summary = request.GET.get('include_block_summary', '1').strip() != '0'
    from_date = request.GET.get('from_date', '').strip()
    to_date = request.GET.get('to_date', '').strip()
    agency = request.GET.get('agency', '').strip()
    block = request.GET.get('block', '').strip()
    room = request.GET.get('room', '').strip()
    status_filter = request.GET.get('status', '').strip()
    q = request.GET.get('q', '').strip()

    columns = []
    rows = []
    block_clean = block.replace('Block ', '').strip() if block else ''

    if module in ['custom_bundle', 'all_master']:
        # Return all exportable sections with metadata
        sections_data = [
            {'id': 'block_summary', 'title': '🏢 Block Master Architecture & Summary', 'desc': 'All 16 blocks capacity, occupancy %, inside present, fans & lights', 'badge': 'Sheet 1 • Overview'},
            {'id': 'room_detail', 'title': '🚪 Room-Wise Kundali & Occupant Register', 'desc': 'Bed-by-bed occupant roster with employee ID, trade, key, bedding assets', 'badge': 'Sheet 2 • Bed Kundali'},
            {'id': 'rooms', 'title': '🏢 Rooms & Bed Capacity Register', 'desc': '324 rooms, capacity, current occupants, vacancy, lock and key status', 'badge': 'Sheet 3 • Rooms'},
            {'id': 'leaves', 'title': '📋 Workforce Leave Records & Applications', 'desc': 'Employees on leave, dates, destinations, contacts, approved by', 'badge': 'Sheet 4 • Leaves'},
            {'id': 'resident_assets', 'title': '🛋️ Resident Bed Assets Allotments', 'desc': 'Worker cot, mattress code, pillow, fan, condition and issue dates', 'badge': 'Sheet 5 • Assets'},
            {'id': 'board_update', 'title': '📊 Official Board Update (Blocks A to P)', 'desc': 'Official architecture and trade counts for national & expatriate workers', 'badge': 'Sheet 6 • Board Update'},
            {'id': 'gate_movements', 'title': '🛡️ Security Gate In / Out Movement Log', 'desc': 'Gate security punch logs, directions, vehicles, purpose of visit', 'badge': 'Sheet 7 • Security'},
            {'id': 'mess_meals', 'title': '🍱 Mess Meal Consumption Logs', 'desc': 'Meal punch logs, timings, mess locations, attendance records', 'badge': 'Sheet 8 • Meals'},
            {'id': 'mess_assets', 'title': '🍳 Mess & Kitchen Asset Register', 'desc': 'Kitchen equipment, staff allocations, facilities, quantities', 'badge': 'Sheet 9 • Kitchen'},
            {'id': 'site_transfers', 'title': '🚚 External Project Site Transfers', 'desc': 'Gate passes, dispatched items, destination sites, vehicle numbers', 'badge': 'Sheet 10 • Transfers'},
            {'id': 'mess_wastage', 'title': '🗑️ Food Wastage & Leftover Register', 'desc': 'Daily meal preparation vs wastage in kg, percentage, caterer remarks', 'badge': 'Sheet 11 • Wastage'},
            {'id': 'mess_feedback', 'title': '💬 Mess Food Quality & Feedback Ratings', 'desc': 'Worker reviews, taste ratings, quality tags, employee suggestions', 'badge': 'Sheet 12 • Ratings'}
        ]
        return JsonResponse({
            'status': 'success',
            'module': module,
            'is_bundle': True,
            'sections': sections_data,
            'count': len(sections_data)
        })

    if module == 'block_summary' and not include_block_summary:
        # User toggled off block summary architecture; show room kundali detail
        module = 'room_detail'

    if module == 'block_summary':
        columns = [
            'Block Code', 'Block Name', 'Category', 'Total Rooms',
            'Total Capacity', 'Current Occupants', 'Vacant Beds', 'Occupancy %',
            'Inside Present', 'Total Fans', 'Total Lights',
            'Caretaker Name', 'Caretaker Contact', 'Top Occupant Trades'
        ]
        b_qs = CampBlock.objects.prefetch_related('rooms').all().order_by('block_code')
        if block:
            b_qs = b_qs.filter(Q(block_code__icontains=block) | Q(name__icontains=block) | Q(block_code__icontains=block_clean))
        if q:
            b_qs = b_qs.filter(Q(block_code__icontains=q) | Q(name__icontains=q) | Q(category__icontains=q))

        for b in b_qs:
            b_rooms = b.rooms.all()
            r_nums = set(r.room_number.strip().upper() for r in b_rooms)
            b_letter = b.block_code.replace('Block ', '').strip()
            emp_qs = Employee.objects.filter(status='Active').filter(
                Q(camp_room__in=r_nums) | Q(camp_room__istartswith=b_letter)
            )
            if agency and agency != 'ALL':
                emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
            if room:
                emp_qs = emp_qs.filter(camp_room__icontains=room)

            tot_rooms = len(b_rooms)
            cap = sum(r.capacity for r in b_rooms) or b.capacity or 0
            occ = emp_qs.count()
            vac = max(0, cap - occ)
            occ_pct = round(occ / cap * 100) if cap > 0 else 0
            inside_cnt = emp_qs.exclude(shift_remarks__icontains='leave').exclude(status='On Leave').count()

            fans = sum(r.fan_count for r in b_rooms)
            lights = sum(r.tubelight_count for r in b_rooms)

            desigs = [e.designation.strip() for e in emp_qs if e.designation]
            trade_counter = Counter(desigs)
            top_trades = [f"{t} ({cnt})" for t, cnt in trade_counter.most_common(2)]
            top_trades_str = ", ".join(top_trades) if top_trades else "Vacant"

            rows.append({
                'id': str(b.id),
                'col1': b.block_code,
                'col2': b.name or f"Block {b.block_code}",
                'col3': b.get_category_display() if hasattr(b, 'get_category_display') else (b.category or 'General'),
                'col4': f"{tot_rooms} Rooms",
                'col5': f"{cap} Beds",
                'col6': str(occ),
                'col7': f"{vac} Beds",
                'col8': f"{occ_pct}%",
                'col9': f"{inside_cnt} Present",
                'col10': f"{fans} Fans",
                'col11': f"{lights} Lights",
                'col12': b.caretaker_name or '-',
                'col13': b.caretaker_contact or '-',
                'col14': top_trades_str
            })

    elif module == 'room_detail':
        columns = [
            'Block', 'Room #', 'Bed #', 'Occupant Name',
            'Employee ID', 'Trade / Designation', 'Agency / Contractor',
            'Nationality', 'Contact Number', 'Room Key Status', 'Key Holder',
            'Cot Allotted', 'Mattress Allotted', 'Pillow Allotted',
            'Fan Allotted', 'Condition', 'Allotment Date'
        ]
        r_qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
        if block:
            r_qs = r_qs.filter(Q(block__block_code__icontains=block) | Q(block__name__icontains=block) | Q(room_number__istartswith=block_clean))
        if room:
            r_qs = r_qs.filter(room_number__icontains=room)
        if q:
            r_qs = r_qs.filter(Q(room_number__icontains=q) | Q(block__block_code__icontains=q) | Q(block__block_name__icontains=q))

        r_list = sorted(list(r_qs), key=lambda r: _natural_sort_key(r.room_number))

        allotments = CampAssetAllotment.objects.select_related('employee', 'room').filter(room__in=r_list)
        if agency and agency != 'ALL':
            allotments = allotments.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL':
            allotments = allotments.filter(condition=status_filter)

        allotments_by_room = defaultdict(list)
        for a in allotments:
            if a.room_id:
                allotments_by_room[a.room_id].append(a)

        emp_qs = Employee.objects.filter(status='Active')
        if agency and agency != 'ALL':
            emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
        emp_by_room = defaultdict(list)
        for e in emp_qs:
            if e.camp_room:
                emp_by_room[e.camp_room.strip().upper()].append(e)

        for r in r_list:
            r_allots = allotments_by_room.get(r.id, [])
            r_allots.sort(key=lambda a: _natural_sort_key(a.bed_number or ''))
            room_emps = emp_by_room.get(r.room_number.strip().upper(), [])
            block_name = r.block.block_code if r.block else 'Camp'
            key_status_disp = r.get_key_status_display()
            key_holder = r.key_issued_to.name if r.key_issued_to else ("In Gate Box" if r.key_status == 'IN_GATE_BOX' else "-")

            if r_allots:
                for a in r_allots:
                    e = a.employee
                    if not e: continue
                    rows.append({
                        'id': f"A_{a.id}",
                        'col1': block_name,
                        'col2': r.room_number,
                        'col3': a.bed_number or 'Bed 1',
                        'col4': e.name,
                        'col5': e.emp_id or f"EMP-{e.id}",
                        'col6': e.designation or 'Worker',
                        'col7': e.contractor_agency or 'Company Direct',
                        'col8': e.nationality or '-',
                        'col9': e.contact_info or '-',
                        'col10': key_status_disp,
                        'col11': key_holder,
                        'col12': 'Yes' if a.bed_cot_allotted else 'No',
                        'col13': 'Yes' if a.mattress_allotted else 'No',
                        'col14': 'Yes' if a.pillow_allotted else 'No',
                        'col15': 'Yes' if a.fan_allotted else 'No',
                        'col16': a.get_condition_display(),
                        'col17': a.issue_date.strftime('%Y-%m-%d') if a.issue_date else '-'
                    })
            elif room_emps:
                for idx, e in enumerate(room_emps, 1):
                    rows.append({
                        'id': f"E_{e.id}",
                        'col1': block_name,
                        'col2': r.room_number,
                        'col3': f"Bed {idx}",
                        'col4': e.name,
                        'col5': e.emp_id or f"EMP-{e.id}",
                        'col6': e.designation or 'Worker',
                        'col7': e.contractor_agency or 'Company Direct',
                        'col8': e.nationality or '-',
                        'col9': e.contact_info or '-',
                        'col10': key_status_disp,
                        'col11': key_holder,
                        'col12': 'Standard Issue',
                        'col13': 'Standard Issue',
                        'col14': 'Standard Issue',
                        'col15': 'Standard Issue',
                        'col16': 'Active',
                        'col17': '-'
                    })
            else:
                if not agency or agency == 'ALL':
                    rows.append({
                        'id': f"R_{r.id}",
                        'col1': block_name,
                        'col2': r.room_number,
                        'col3': f"0 / {r.capacity} Beds",
                        'col4': '(Vacant Room)',
                        'col5': '-',
                        'col6': '-',
                        'col7': '-',
                        'col8': '-',
                        'col9': '-',
                        'col10': key_status_disp,
                        'col11': key_holder,
                        'col12': 'Available',
                        'col13': 'Available',
                        'col14': 'Available',
                        'col15': f"{r.fan_count} Fans",
                        'col16': r.get_door_lock_status_display(),
                        'col17': '-'
                    })

    elif module == 'rooms':
        columns = [
            'Block', 'Room #', 'Bed Capacity', 'Current Occupants',
            'Vacant Beds', 'Occupancy %', 'Key Status', 'Key Holder',
            'Fan Count', 'Fan Status', 'Tubelight Count', 'Tubelight Status',
            'Door Lock Status', 'Residents Roster'
        ]
        qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
        if block:
            qs = qs.filter(Q(block__block_code__icontains=block) | Q(block__name__icontains=block) | Q(room_number__istartswith=block_clean))
        if room:
            qs = qs.filter(room_number__icontains=room)
        if q:
            qs = qs.filter(Q(room_number__icontains=q) | Q(block__block_code__icontains=q) | Q(block__block_name__icontains=q))

        r_list = sorted(list(qs), key=lambda r: _natural_sort_key(r.room_number))

        emp_qs = Employee.objects.filter(status='Active')
        if agency and agency != 'ALL':
            emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
        res_map = defaultdict(list)
        for emp in emp_qs:
            if emp.camp_room:
                res_map[emp.camp_room.strip().upper()].append(emp)

        for r in r_list:
            r_key = r.room_number.strip().upper()
            res_list = res_map.get(r_key, [])
            occ = len(res_list)
            if agency and agency != 'ALL' and occ == 0:
                continue
            vacant = max(0, r.capacity - occ)
            occ_pct = round(occ / r.capacity * 100) if r.capacity else 0
            key_holder = r.key_issued_to.name if r.key_issued_to else ("In Gate Box" if r.key_status == 'IN_GATE_BOX' else "-")
            res_str = ", ".join([f"{e.name} ({e.emp_id or 'ID#'+str(e.id)})" for e in res_list]) if res_list else "None (Vacant)"
            rows.append({
                'id': str(r.id),
                'col1': r.block.block_code if r.block else 'Camp',
                'col2': r.room_number,
                'col3': f"{r.capacity} Beds",
                'col4': str(occ),
                'col5': f"{vacant} Beds",
                'col6': f"{occ_pct}%",
                'col7': r.get_key_status_display(),
                'col8': key_holder,
                'col9': f"{r.fan_count} Fans",
                'col10': r.get_fan_status_display(),
                'col11': f"{r.tubelight_count} Lights",
                'col12': r.get_tubelight_status_display(),
                'col13': r.get_door_lock_status_display(),
                'col14': res_str
            })

    elif module == 'leaves':
        columns = [
            'Form Number', 'Leave Type', 'Employee ID', 'Worker Name',
            'Agency / Contractor', 'Designation', 'Department',
            'Start Date', 'End Date', 'Total Days', 'Actual Return Date',
            'Leave Category', 'Reason', 'Destination', 'Contact Phone',
            'Replacement Worker', 'Status', 'Approved By', 'Application Date', 'Remarks'
        ]
        if EmployeeLeaveRecord.objects.exists():
            qs = EmployeeLeaveRecord.objects.select_related('employee', 'created_by').all().order_by('-start_date')
            if from_date: qs = qs.filter(end_date__gte=from_date)
            if to_date: qs = qs.filter(start_date__lte=to_date)
            if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
            if block: qs = qs.filter(employee__camp_room__icontains=block_clean)
            if room: qs = qs.filter(employee__camp_room__icontains=room)
            if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
            if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(form_number__icontains=q))

            for lv in qs:
                rows.append({
                    'id': str(lv.id),
                    'col1': lv.form_number or f"LV-{lv.id:04d}",
                    'col2': lv.get_leave_type_display(),
                    'col3': lv.employee.emp_id or f"EMP-{lv.employee.id}",
                    'col4': lv.employee.name,
                    'col5': lv.employee.contractor_agency or "Company Direct",
                    'col6': lv.employee.designation or "-",
                    'col7': lv.employee.department or "-",
                    'col8': lv.start_date.strftime('%Y-%m-%d') if lv.start_date else '-',
                    'col9': lv.end_date.strftime('%Y-%m-%d') if lv.end_date else '-',
                    'col10': str(lv.total_days),
                    'col11': lv.actual_return_date.strftime('%Y-%m-%d') if lv.actual_return_date else '-',
                    'col12': lv.get_category_display(),
                    'col13': lv.reason or "-",
                    'col14': lv.destination or "-",
                    'col15': lv.contact_number or "-",
                    'col16': lv.replacement_worker.name if lv.replacement_worker else "-",
                    'col17': lv.get_status_display(),
                    'col18': lv.approved_by or "-",
                    'col19': lv.created_at.strftime('%Y-%m-%d') if lv.created_at else '-',
                    'col20': lv.remarks or "-"
                })
        else:
            emp_qs = Employee.objects.filter(Q(status='On Leave') | Q(shift_remarks__icontains='leave')).order_by('name')
            if agency and agency != 'ALL': emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
            if block: emp_qs = emp_qs.filter(camp_room__icontains=block_clean)
            if room: emp_qs = emp_qs.filter(camp_room__icontains=room)
            if q: emp_qs = emp_qs.filter(Q(name__icontains=q) | Q(emp_id__icontains=q) | Q(shift_remarks__icontains=q))

            for e in emp_qs:
                rows.append({
                    'id': str(e.id),
                    'col1': f"LV-EMP-{e.id}",
                    'col2': "Regular Leave",
                    'col3': e.emp_id or f"EMP-{e.id}",
                    'col4': e.name,
                    'col5': e.contractor_agency or "Company Direct",
                    'col6': e.designation or "-",
                    'col7': e.department or "-",
                    'col8': from_date or "-",
                    'col9': to_date or "-",
                    'col10': "-",
                    'col11': "-",
                    'col12': "Annual / Personal",
                    'col13': e.shift_remarks or "Authorized Leave",
                    'col14': e.camp_room or "Outside",
                    'col15': e.contact_info or "-",
                    'col16': "-",
                    'col17': e.status,
                    'col18': "-",
                    'col19': "-",
                    'col20': e.shift_remarks or "-"
                })

    elif module == 'resident_assets':
        columns = [
            'Room #', 'Bed #', 'Worker Name', 'Employee ID',
            'Contractor / Agency', 'Cot Allotted', 'Mattress Allotted',
            'Mattress Barcode', 'Pillow Allotted', 'Fan Allotted',
            'Blanket Allotted', 'Bucket & Mug', 'Locker Key Issued',
            'Locker #', 'Asset Condition', 'Allotment Date'
        ]
        qs = CampAssetAllotment.objects.select_related('employee', 'room').all()
        if block:
            qs = qs.filter(Q(room__block__block_code__icontains=block) | Q(employee__camp_room__istartswith=block_clean))
        if room:
            qs = qs.filter(Q(room__room_number__icontains=room) | Q(employee__camp_room__icontains=room))
        if agency and agency != 'ALL':
            qs = qs.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL':
            qs = qs.filter(condition=status_filter)
        if from_date:
            qs = qs.filter(issue_date__gte=from_date)
        if to_date:
            qs = qs.filter(issue_date__lte=to_date)
        if q:
            qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(room__room_number__icontains=q))

        a_list = sorted(list(qs), key=lambda a: (_natural_sort_key(a.room.room_number if a.room else (a.employee.camp_room if a.employee else '')), _natural_sort_key(a.bed_number or '')))

        for a in a_list:
            r_no = a.room.room_number if a.room else (a.employee.camp_room or '-')
            rows.append({
                'id': str(a.id),
                'col1': r_no,
                'col2': a.bed_number or 'Bed 1',
                'col3': a.employee.name,
                'col4': a.employee.emp_id or f"EMP-{a.employee.id}",
                'col5': a.employee.contractor_agency or "Company Direct",
                'col6': "Yes" if a.bed_cot_allotted else "No",
                'col7': "Yes" if a.mattress_allotted else "No",
                'col8': a.mattress_code or "-",
                'col9': "Yes" if a.pillow_allotted else "No",
                'col10': "Yes" if a.fan_allotted else "No",
                'col11': "Yes" if a.blanket_allotted else "No",
                'col12': "Yes" if a.bucket_mug_issued else "No",
                'col13': "Yes" if a.locker_key_issued else "No",
                'col14': a.locker_number or "-",
                'col15': a.get_condition_display(),
                'col16': a.issue_date.strftime('%Y-%m-%d') if a.issue_date else '-'
            })

    elif module == 'mess_assets':
        columns = [
            'Asset Name', 'Asset Code', 'Category', 'Location / Facility',
            'Recipient Name', 'Recipient Role', 'Phone Number',
            'Quantity Allocated', 'Issue Date', 'Expected Return', 'Current Status', 'Notes / Remarks'
        ]
        qs = MessAssetAllocation.objects.exclude(allocated_to_type='OTHER_SITE').select_related('asset', 'staff_member', 'mess_location').all().order_by('-created_at')
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(asset__name__icontains=q) | Q(asset__asset_code__icontains=q) | Q(staff_name__icontains=q) | Q(location_name__icontains=q))
        for m in qs:
            rows.append({
                'id': str(m.id),
                'col1': m.asset.name if m.asset else '-',
                'col2': m.asset.asset_code if m.asset else '-',
                'col3': m.asset.get_category_display() if m.asset else '-',
                'col4': m.location_display,
                'col5': m.recipient_name,
                'col6': m.get_allocated_to_type_display(),
                'col7': m.handover_phone or '-',
                'col8': f"{m.quantity} {m.asset.unit if m.asset else 'Pcs'}",
                'col9': m.issue_date.strftime('%Y-%m-%d') if m.issue_date else '-',
                'col10': m.expected_return_date.strftime('%Y-%m-%d') if m.expected_return_date else '-',
                'col11': m.get_status_display(),
                'col12': m.remarks or '-'
            })

    elif module == 'site_transfers':
        columns = [
            'Gate Pass #', 'Destination Site', 'Vehicle Number', 'Driver / Contact',
            'Recipient Officer', 'Asset Name', 'Asset Code', 'Quantity Dispatched',
            'Dispatch Date', 'Expected Return', 'Status', 'Dispatch Remarks'
        ]
        qs = MessAssetAllocation.objects.filter(allocated_to_type='OTHER_SITE').select_related('asset').all().order_by('-created_at')
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(location_name__icontains=q) | Q(handover_to__icontains=q) | Q(vehicle_number__icontains=q) | Q(gate_pass_no__icontains=q))
        for t in qs:
            rows.append({
                'id': str(t.id),
                'col1': t.gate_pass_no or '-',
                'col2': t.location_name or 'External Site',
                'col3': t.vehicle_number or '-',
                'col4': t.handover_to or '-',
                'col5': t.staff_name or '-',
                'col6': t.asset.name if t.asset else '-',
                'col7': t.asset.asset_code if t.asset else '-',
                'col8': f"{t.quantity} {t.asset.unit if t.asset else 'Pcs'}",
                'col9': t.issue_date.strftime('%Y-%m-%d') if t.issue_date else '-',
                'col10': t.expected_return_date.strftime('%Y-%m-%d') if t.expected_return_date else '-',
                'col11': t.get_status_display(),
                'col12': t.remarks or '-'
            })

    elif module == 'mess_meals':
        columns = [
            'Date', 'Punch Time', 'Employee ID', 'Worker Name',
            'Contractor / Agency', 'Mess Location', 'Meal Window',
            'Status', 'Entry Mode', 'Remarks'
        ]
        qs = MessLog.objects.select_related('employee', 'mess_location').all().order_by('-punch_time')
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(mess_location__name__icontains=q))
        for l in qs[:1000]:
            rows.append({
                'id': str(l.id),
                'col1': l.date.strftime('%Y-%m-%d') if l.date else '-',
                'col2': l.punch_time.strftime('%H:%M:%S') if l.punch_time else '-',
                'col3': l.employee.emp_id or f"EMP-{l.employee.id}",
                'col4': l.employee.name,
                'col5': l.employee.contractor_agency or "Company Direct",
                'col6': l.mess_location.name if l.mess_location else "Main Canteen",
                'col7': l.get_meal_type_display(),
                'col8': l.status,
                'col9': l.entry_mode or 'SCANNER',
                'col10': l.remarks or '-'
            })

    elif module == 'mess_wastage':
        columns = [
            'Date', 'Meal Type', 'Mess Facility', 'Prepared (Kg)',
            'Wasted (Kg)', 'Wastage %',
            'Caterer / Contractor', 'Remarks / Reasons'
        ]
        qs = MessWastageLog.objects.select_related('mess_location').all().order_by('-date')
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if q: qs = qs.filter(Q(contractor_name__icontains=q) | Q(remarks__icontains=q) | Q(mess_location__name__icontains=q))
        for w in qs:
            pct = f"{round((w.wasted_qty_kg / w.prepared_qty_kg * 100), 1)}%" if w.prepared_qty_kg > 0 else "0%"
            rows.append({
                'id': str(w.id),
                'col1': w.date.strftime('%Y-%m-%d') if w.date else '-',
                'col2': w.meal_type,
                'col3': w.mess_location.name if w.mess_location else "Central Mess",
                'col4': f"{w.prepared_qty_kg} kg",
                'col5': f"{w.wasted_qty_kg} kg",
                'col6': pct,
                'col7': w.contractor_name or '-',
                'col8': w.remarks or '-'
            })

    elif module == 'mess_feedback':
        columns = [
            'Date', 'Meal Window', 'Worker Name', 'Employee ID',
            'Agency', 'Rating / Feedback Tag', 'Comments & Suggestions'
        ]
        qs = MessFeedback.objects.select_related('employee').all().order_by('-created_at')
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(comments__icontains=q))
        for fb in qs:
            rows.append({
                'id': str(fb.id),
                'col1': fb.date.strftime('%Y-%m-%d') if fb.date else '-',
                'col2': fb.get_meal_type_display(),
                'col3': fb.employee.name if fb.employee else "Anonymous Worker",
                'col4': fb.employee.emp_id or (f"EMP-{fb.employee.id}" if fb.employee else "-"),
                'col5': fb.employee.contractor_agency or "Company" if fb.employee else "-",
                'col6': fb.get_feedback_tag_display(),
                'col7': fb.comments or '-'
            })

    elif module == 'board_update':
        columns = ['Block Code', 'Block Name', 'Category', 'Total Rooms', 'Active Residents']
        qs = CampBlock.objects.prefetch_related('rooms').all().order_by('block_code')
        if block:
            qs = qs.filter(Q(block_code__icontains=block) | Q(name__icontains=block) | Q(block_code__icontains=block_clean))
        if q: qs = qs.filter(Q(block_code__icontains=q) | Q(name__icontains=q))
        for b in qs:
            b_letter = b.block_code.replace('Block ', '').strip()
            res_cnt = Employee.objects.filter(
                Q(camp_room__istartswith=b_letter) | Q(camp_room__istartswith=b.block_code),
                status='Active'
            ).count()
            rows.append({
                'id': str(b.id),
                'col1': b.block_code,
                'col2': b.name or f"Block {b.block_code}",
                'col3': b.get_category_display() if hasattr(b, 'get_category_display') else (b.category or 'General'),
                'col4': f"{b.rooms.count()} Rooms",
                'col5': f"{res_cnt} Residents"
            })

    elif module == 'gate_movements':
        columns = ['Timestamp', 'Direction', 'Worker Name', 'Emp ID', 'Gate', 'Purpose']
        qs = CampMovementLog.objects.select_related('employee').all().order_by('-timestamp')
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(gate_name__icontains=q))
        for g in qs[:300]:
            rows.append({
                'id': str(g.id),
                'col1': g.timestamp.strftime('%Y-%m-%d %H:%M') if g.timestamp else '-',
                'col2': g.direction,
                'col3': g.employee.name,
                'col4': g.employee.emp_id or f"EMP-{g.employee.id}",
                'col5': g.gate_name or 'Main Gate',
                'col6': g.purpose or '-'
            })

    return JsonResponse({
        'status': 'success',
        'module': module,
        'columns': columns,
        'rows': rows,
        'count': len(rows)
    })


@login_required
def export_camp_universal_excel(request):
    """
    Universal Data Export Engine for Camp & Mess Management System.
    Supports multi-section checkbox selection (sections parameter),
    granular row selection (selected_ids), natural numerical room sorting,
    block filtering, and room filtering.
    """
    import os
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from collections import defaultdict, Counter
    from django.http import HttpResponse
    from django.db.models import Q
    from django.utils import timezone
    from portal.models import (
        CampRoom, CampBlock, Employee, EmployeeLeaveRecord,
        CampAssetAllotment, MessAssetItem, MessAssetAllocation,
        MessLog, MessWastageLog, MessFeedback, MessMenu, MessMealWindow
    )

    module = request.GET.get('module', 'all_master').strip().lower()
    sections_str = request.GET.get('sections', '').strip()
    selected_sections = [s.strip().lower() for s in sections_str.split(',') if s.strip()] if sections_str else []

    from_date = request.GET.get('from_date', '').strip()
    to_date = request.GET.get('to_date', '').strip()
    agency = request.GET.get('agency', '').strip()
    block_filter = request.GET.get('block', '').strip()
    room_filter = request.GET.get('room', '').strip()
    status_filter = request.GET.get('status', '').strip()
    q = request.GET.get('q', '').strip()
    selected_ids_str = request.GET.get('selected_ids', '').strip()
    selected_ids = [s.strip() for s in selected_ids_str.split(',') if s.strip()] if selected_ids_str else None

    block_clean = block_filter.replace('Block ', '').strip() if block_filter else ''
    include_block_summary = request.GET.get('include_block_summary', '1').strip() != '0'
    cols_str = request.GET.get('cols', '').strip()
    selected_cols = [c.strip() for c in cols_str.split(',') if c.strip()] if cols_str else None

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Calibri", size=10, color="0F172A")
    center_align = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")

    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )
    alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    def filter_columns(headers, alignments=None):
        if not selected_cols:
            return headers, alignments, list(range(len(headers)))
        # Normalize header strings for comparison
        selected_set = set(c.upper().strip() for c in selected_cols)
        indices = []
        for idx, h in enumerate(headers):
            h_clean = h.upper().strip()
            # Match exact or normalized substring
            if any(sel in h_clean or h_clean in sel for sel in selected_set):
                indices.append(idx)
        if not indices:
            indices = list(range(len(headers)))
        filtered_headers = [headers[i] for i in indices]
        filtered_alignments = [alignments[i] for i in indices] if alignments else None
        return filtered_headers, filtered_alignments, indices

    def style_worksheet(ws, headers, alignments=None):
        ws.views.sheetView[0].showGridLines = True
        for col_idx, col_name in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center_align
            cell.border = thin_border

        for row_idx, row in enumerate(ws.iter_rows(min_row=2), 2):
            is_alt = (row_idx % 2 == 0)
            for col_idx, cell in enumerate(row, 1):
                cell.font = data_font
                cell.border = thin_border
                if is_alt:
                    cell.fill = alt_fill
                if alignments and col_idx - 1 < len(alignments):
                    cell.alignment = alignments[col_idx - 1]
                else:
                    cell.alignment = left_align

        for col in ws.columns:
            max_len = 0
            for cell in col:
                val = str(cell.value or '')
                if '\n' in val:
                    val = max(val.split('\n'), key=len)
                if len(val) > max_len:
                    max_len = len(val)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 13)

    wb = openpyxl.Workbook()
    is_first_sheet = True

    def get_or_create_sheet(title):
        nonlocal is_first_sheet
        if is_first_sheet:
            ws = wb.active
            ws.title = title
            is_first_sheet = False
            return ws
        else:
            return wb.create_sheet(title=title)

    # 1. Block-Wise Summary
    def populate_block_summary(ws):
        headers = [
            "BLOCK CODE", "BLOCK NAME", "CATEGORY", "TOTAL ROOMS",
            "TOTAL CAPACITY", "CURRENT OCCUPANTS", "VACANT BEDS", "OCCUPANCY %",
            "INSIDE PRESENT", "TOTAL FANS", "TOTAL LIGHTS",
            "CARETAKER NAME", "CARETAKER CONTACT", "TOP OCCUPANT TRADES"
        ]
        alignments = [
            center_align, left_align, center_align, center_align,
            center_align, center_align, center_align, center_align,
            center_align, center_align, center_align,
            left_align, center_align, left_align
        ]
        filt_headers, filt_alignments, indices = filter_columns(headers, alignments) if module == 'block_summary' else (headers, alignments, list(range(len(headers))))
        ws.append(filt_headers)

        b_qs = CampBlock.objects.prefetch_related('rooms').all().order_by('block_code')
        if selected_ids and module == 'block_summary':
            b_qs = b_qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if block_filter:
            b_qs = b_qs.filter(Q(block_code__icontains=block_filter) | Q(name__icontains=block_filter) | Q(block_code__icontains=block_clean))
        if q:
            b_qs = b_qs.filter(Q(block_code__icontains=q) | Q(name__icontains=q) | Q(category__icontains=q))

        for b in b_qs:
            b_rooms = sorted(b.rooms.all(), key=lambda r: _natural_sort_key(r.room_number))
            r_nums = set(r.room_number.strip().upper() for r in b_rooms)
            b_letter = b.block_code.replace('Block ', '').strip()
            emp_qs = Employee.objects.filter(status='Active').filter(
                Q(camp_room__in=r_nums) | Q(camp_room__istartswith=b_letter)
            )
            if agency and agency != 'ALL':
                emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
            if room_filter:
                emp_qs = emp_qs.filter(camp_room__icontains=room_filter)

            tot_rooms = len(b_rooms)
            cap = sum(r.capacity for r in b_rooms) or b.capacity or 0
            occ = emp_qs.count()
            vac = max(0, cap - occ)
            occ_pct = f"{round(occ / cap * 100)}%" if cap > 0 else "0%"
            inside_cnt = emp_qs.exclude(shift_remarks__icontains='leave').exclude(status='On Leave').count()

            fans = sum(r.fan_count for r in b_rooms)
            lights = sum(r.tubelight_count for r in b_rooms)

            desigs = [e.designation.strip() for e in emp_qs if e.designation]
            trade_counter = Counter(desigs)
            top_trades = [f"{t} ({cnt})" for t, cnt in trade_counter.most_common(3)]
            top_trades_str = ", ".join(top_trades) if top_trades else "Vacant"

            row_vals = [
                b.block_code,
                b.name or f"Block {b.block_code}",
                b.get_category_display() if hasattr(b, 'get_category_display') else (b.category or 'General'),
                tot_rooms,
                cap,
                occ,
                vac,
                occ_pct,
                inside_cnt,
                fans,
                lights,
                b.caretaker_name or '-',
                b.caretaker_contact or '-',
                top_trades_str
            ]
            ws.append([row_vals[i] for i in indices])

        style_worksheet(ws, filt_headers, filt_alignments)

    # 2. Room-Wise Detail Kundali (Naturally Sorted)
    def populate_room_detail(ws):
        headers = [
            "BLOCK", "ROOM NUMBER", "BED NUMBER", "OCCUPANT NAME",
            "EMPLOYEE ID", "TRADE / DESIGNATION", "AGENCY / CONTRACTOR",
            "NATIONALITY", "CONTACT NUMBER", "ROOM KEY STATUS", "KEY HOLDER",
            "COT ALLOTTED", "MATTRESS ALLOTTED", "PILLOW ALLOTTED",
            "FAN ALLOTTED", "CONDITION", "ALLOTMENT DATE"
        ]
        ws.append(headers)
        alignments = [
            center_align, center_align, center_align, left_align,
            center_align, left_align, left_align,
            center_align, center_align, center_align, left_align,
            center_align, center_align, center_align,
            center_align, center_align, center_align
        ]

        r_qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
        if block_filter:
            r_qs = r_qs.filter(Q(block__block_code__icontains=block_filter) | Q(block__name__icontains=block_filter) | Q(room_number__istartswith=block_clean))
        if room_filter:
            r_qs = r_qs.filter(room_number__icontains=room_filter)
        if q:
            r_qs = r_qs.filter(Q(room_number__icontains=q) | Q(block__block_code__icontains=q) | Q(block__block_name__icontains=q))

        r_list = sorted(list(r_qs), key=lambda r: _natural_sort_key(r.room_number))

        allotments = CampAssetAllotment.objects.select_related('employee', 'room').filter(room__in=r_list)
        if agency and agency != 'ALL':
            allotments = allotments.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL':
            allotments = allotments.filter(condition=status_filter)

        allotments_by_room = defaultdict(list)
        for a in allotments:
            if a.room_id:
                allotments_by_room[a.room_id].append(a)

        emp_qs = Employee.objects.filter(status='Active')
        if agency and agency != 'ALL':
            emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
        emp_by_room = defaultdict(list)
        for e in emp_qs:
            if e.camp_room:
                emp_by_room[e.camp_room.strip().upper()].append(e)

        for r in r_list:
            r_allots = allotments_by_room.get(r.id, [])
            r_allots.sort(key=lambda a: _natural_sort_key(a.bed_number or ''))
            room_emps = emp_by_room.get(r.room_number.strip().upper(), [])
            block_name = r.block.block_code if r.block else 'Camp'
            key_status_disp = r.get_key_status_display()
            key_holder = r.key_issued_to.name if r.key_issued_to else ("In Gate Box" if r.key_status == 'IN_GATE_BOX' else "-")

            if r_allots:
                for a in r_allots:
                    e = a.employee
                    if not e: continue
                    if selected_ids and module in ['room_detail', 'block_summary']:
                        if f"A_{a.id}" not in selected_ids and str(a.id) not in selected_ids:
                            continue
                    ws.append([
                        block_name,
                        r.room_number,
                        a.bed_number or 'Bed 1',
                        e.name,
                        e.emp_id or f"EMP-{e.id}",
                        e.designation or 'Worker',
                        e.contractor_agency or 'Company Direct',
                        e.nationality or '-',
                        e.contact_info or '-',
                        key_status_disp,
                        key_holder,
                        "Yes" if a.bed_cot_allotted else "No",
                        "Yes" if a.mattress_allotted else "No",
                        "Yes" if a.pillow_allotted else "No",
                        "Yes" if a.fan_allotted else "No",
                        a.get_condition_display(),
                        a.issue_date.strftime('%Y-%m-%d') if a.issue_date else '-'
                    ])
            elif room_emps:
                for idx, e in enumerate(room_emps, 1):
                    if selected_ids and module in ['room_detail', 'block_summary']:
                        if f"E_{e.id}" not in selected_ids and str(e.id) not in selected_ids:
                            continue
                    ws.append([
                        block_name,
                        r.room_number,
                        f"Bed {idx}",
                        e.name,
                        e.emp_id or f"EMP-{e.id}",
                        e.designation or 'Worker',
                        e.contractor_agency or 'Company Direct',
                        e.nationality or '-',
                        e.contact_info or '-',
                        key_status_disp,
                        key_holder,
                        "Standard", "Standard", "Standard", "Standard",
                        "Active",
                        "-"
                    ])
            else:
                if not agency or agency == 'ALL':
                    if selected_ids and module in ['room_detail', 'block_summary']:
                        if f"R_{r.id}" not in selected_ids and str(r.id) not in selected_ids:
                            continue
                    ws.append([
                        block_name,
                        r.room_number,
                        f"0 / {r.capacity} Beds",
                        "(Vacant Room)",
                        "-", "-", "-", "-", "-",
                        key_status_disp,
                        key_holder,
                        "Available", "Available", "Available",
                        f"Fans: {r.fan_count}",
                        r.get_door_lock_status_display(),
                        "-"
                    ])

        style_worksheet(ws, headers, alignments)

    # 3. Rooms & Occupancy Summary (Naturally Sorted)
    def populate_rooms(ws):
        qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
        if selected_ids and module == 'rooms':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if block_filter:
            qs = qs.filter(Q(block__block_code__icontains=block_filter) | Q(block__name__icontains=block_filter) | Q(room_number__istartswith=block_clean))
        if room_filter:
            qs = qs.filter(room_number__icontains=room_filter)
        if q:
            qs = qs.filter(Q(room_number__icontains=q) | Q(block__block_name__icontains=q) | Q(block__block_code__icontains=q))

        r_list = sorted(list(qs), key=lambda r: _natural_sort_key(r.room_number))

        emp_qs = Employee.objects.filter(status='Active')
        if agency and agency != 'ALL':
            emp_qs = emp_qs.filter(contractor_agency__icontains=agency)

        residents_by_room = defaultdict(list)
        for emp in emp_qs:
            if emp.camp_room:
                residents_by_room[emp.camp_room.strip().upper()].append(emp)

        headers = [
            "BLOCK", "ROOM NUMBER", "BED CAPACITY", "CURRENT OCCUPANTS",
            "VACANT BEDS", "OCCUPANCY %", "KEY STATUS", "KEY HOLDER",
            "FAN COUNT", "FAN STATUS", "TUBELIGHT COUNT", "TUBELIGHT STATUS",
            "DOOR LOCK STATUS", "RESIDENTS ROSTER (NAME, ID, AGENCY)"
        ]
        ws.append(headers)

        alignments = [
            center_align, center_align, center_align, center_align,
            center_align, center_align, center_align, left_align,
            center_align, center_align, center_align, center_align,
            center_align, left_align
        ]

        for r in r_list:
            room_key = r.room_number.strip().upper()
            res_list = residents_by_room.get(room_key, [])
            occ_count = len(res_list)
            vacant = max(0, r.capacity - occ_count)
            occ_pct = f"{round((occ_count / r.capacity * 100))}%" if r.capacity > 0 else "0%"

            if agency and agency != 'ALL' and occ_count == 0:
                continue

            res_str = ", ".join([f"{e.name} ({e.emp_id or 'ID#'+str(e.id)} - {e.contractor_agency or 'Company'})" for e in res_list]) if res_list else "None (Vacant)"
            key_holder = r.key_issued_to.name if r.key_issued_to else ("In Gate Box" if r.key_status == 'IN_GATE_BOX' else ("Key Lost" if r.key_status == 'LOST' else "-"))

            ws.append([
                r.block.block_code if r.block else "Main Camp",
                r.room_number,
                r.capacity,
                occ_count,
                vacant,
                occ_pct,
                r.get_key_status_display(),
                key_holder,
                r.fan_count,
                r.get_fan_status_display(),
                r.tubelight_count,
                r.get_tubelight_status_display(),
                r.get_door_lock_status_display(),
                res_str
            ])

        style_worksheet(ws, headers, alignments)

    # 4. Leaves
    def populate_leaves(ws):
        headers = [
            "FORM NUMBER", "LEAVE TYPE", "EMPLOYEE ID", "WORKER NAME",
            "AGENCY / CONTRACTOR", "DESIGNATION", "DEPARTMENT",
            "START DATE", "END DATE", "TOTAL DAYS", "ACTUAL RETURN DATE",
            "LEAVE CATEGORY", "REASON", "DESTINATION", "CONTACT PHONE",
            "REPLACEMENT WORKER", "STATUS", "APPROVED BY", "APPLICATION DATE", "REMARKS"
        ]
        ws.append(headers)

        alignments = [
            center_align, center_align, center_align, left_align,
            left_align, left_align, left_align,
            center_align, center_align, center_align, center_align,
            left_align, left_align, left_align, center_align,
            left_align, center_align, left_align, center_align, left_align
        ]

        if EmployeeLeaveRecord.objects.exists():
            qs = EmployeeLeaveRecord.objects.select_related('employee', 'created_by').all().order_by('-start_date')
            if selected_ids and module == 'leaves':
                qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
            if from_date: qs = qs.filter(end_date__gte=from_date)
            if to_date: qs = qs.filter(start_date__lte=to_date)
            if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
            if block_filter: qs = qs.filter(employee__camp_room__icontains=block_clean)
            if room_filter: qs = qs.filter(employee__camp_room__icontains=room_filter)
            if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
            if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(form_number__icontains=q))

            for lv in qs:
                ws.append([
                    lv.form_number or f"LV-{lv.id:04d}",
                    lv.get_leave_type_display(),
                    lv.employee.emp_id or f"EMP-{lv.employee.id}",
                    lv.employee.name,
                    lv.employee.contractor_agency or "Company Direct",
                    lv.employee.designation or "-",
                    lv.employee.department or "-",
                    lv.start_date.strftime('%Y-%m-%d') if lv.start_date else '-',
                    lv.end_date.strftime('%Y-%m-%d') if lv.end_date else '-',
                    lv.total_days,
                    lv.actual_return_date.strftime('%Y-%m-%d') if lv.actual_return_date else '-',
                    lv.get_category_display(),
                    lv.reason or "-",
                    lv.destination or "-",
                    lv.contact_number or "-",
                    lv.replacement_worker.name if lv.replacement_worker else "-",
                    lv.get_status_display(),
                    lv.approved_by or "-",
                    lv.created_at.strftime('%Y-%m-%d') if lv.created_at else '-',
                    lv.remarks or "-"
                ])
        else:
            emp_qs = Employee.objects.filter(Q(status='On Leave') | Q(shift_remarks__icontains='leave')).order_by('name')
            if selected_ids and module == 'leaves':
                emp_qs = emp_qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
            if agency and agency != 'ALL': emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
            if block_filter: emp_qs = emp_qs.filter(camp_room__icontains=block_clean)
            if room_filter: emp_qs = emp_qs.filter(camp_room__icontains=room_filter)
            if q: emp_qs = emp_qs.filter(Q(name__icontains=q) | Q(emp_id__icontains=q) | Q(shift_remarks__icontains=q))

            for e in emp_qs:
                ws.append([
                    f"LV-EMP-{e.id}",
                    "Regular Leave",
                    e.emp_id or f"EMP-{e.id}",
                    e.name,
                    e.contractor_agency or "Company Direct",
                    e.designation or "-",
                    e.department or "-",
                    from_date or "-",
                    to_date or "-",
                    "-",
                    "-",
                    "Annual / Personal",
                    e.shift_remarks or "Authorized Leave",
                    e.camp_room or "Outside",
                    e.contact_info or "-",
                    "-",
                    e.status,
                    "HR Desk",
                    "-",
                    "-"
                ])

        style_worksheet(ws, headers, alignments)

    # 5. Resident Bed Assets (Naturally Sorted)
    def populate_resident_assets(ws):
        headers = [
            "ROOM NUMBER", "BED NUMBER", "WORKER NAME", "EMPLOYEE ID",
            "CONTRACTOR / AGENCY", "COT ALLOTTED", "MATTRESS ALLOTTED",
            "MATTRESS BARCODE", "PILLOW ALLOTTED", "FAN ALLOTTED",
            "BLANKET ALLOTTED", "BUCKET & MUG", "LOCKER KEY ISSUED",
            "LOCKER NUMBER", "ASSET CONDITION", "ALLOTMENT DATE"
        ]
        ws.append(headers)

        alignments = [
            center_align, center_align, left_align, center_align,
            left_align, center_align, center_align,
            center_align, center_align, center_align,
            center_align, center_align, center_align,
            center_align, center_align, center_align
        ]

        qs = CampAssetAllotment.objects.select_related('employee', 'room').all()
        if selected_ids and module == 'resident_assets':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if block_filter:
            qs = qs.filter(Q(room__block__block_code__icontains=block_filter) | Q(employee__camp_room__istartswith=block_clean))
        if room_filter:
            qs = qs.filter(Q(room__room_number__icontains=room_filter) | Q(employee__camp_room__icontains=room_filter))
        if agency and agency != 'ALL':
            qs = qs.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL':
            qs = qs.filter(condition=status_filter)
        if from_date:
            qs = qs.filter(issue_date__gte=from_date)
        if to_date:
            qs = qs.filter(issue_date__lte=to_date)
        if q:
            qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(room__room_number__icontains=q))

        a_list = sorted(list(qs), key=lambda a: (_natural_sort_key(a.room.room_number if a.room else (a.employee.camp_room if a.employee else '')), _natural_sort_key(a.bed_number or '')))

        for a in a_list:
            r_no = a.room.room_number if a.room else (a.employee.camp_room or '-')
            ws.append([
                r_no,
                a.bed_number or "Bed 1",
                a.employee.name,
                a.employee.emp_id or f"EMP-{a.employee.id}",
                a.employee.contractor_agency or "Company Direct",
                "Yes" if a.bed_cot_allotted else "No",
                "Yes" if a.mattress_allotted else "No",
                a.mattress_code or "-",
                "Yes" if a.pillow_allotted else "No",
                "Yes" if a.fan_allotted else "No",
                "Yes" if a.blanket_allotted else "No",
                "Yes" if a.bucket_mug_issued else "No",
                "Yes" if a.locker_key_issued else "No",
                a.locker_number or "-",
                a.get_condition_display(),
                a.issue_date.strftime('%Y-%m-%d') if a.issue_date else '-'
            ])

        style_worksheet(ws, headers, alignments)

    # 6. Mess Assets
    def populate_mess_assets(ws):
        headers = [
            "ASSET NAME", "ASSET CODE", "CATEGORY", "TARGET FACILITY",
            "RECIPIENT NAME", "RECIPIENT ROLE", "PHONE NUMBER",
            "QUANTITY ALLOCATED", "ISSUE DATE", "EXPECTED RETURN", "CURRENT STATUS", "NOTES / REMARKS"
        ]
        ws.append(headers)
        alignments = [
            left_align, center_align, center_align, left_align,
            left_align, left_align, center_align,
            center_align, center_align, center_align, center_align, left_align
        ]

        qs = MessAssetAllocation.objects.exclude(allocated_to_type='OTHER_SITE').select_related('asset', 'staff_member', 'mess_location').all().order_by('-created_at')
        if selected_ids and module == 'mess_assets':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(asset__name__icontains=q) | Q(asset__asset_code__icontains=q) | Q(staff_name__icontains=q) | Q(location_name__icontains=q))

        for m in qs:
            ws.append([
                m.asset.name if m.asset else "-",
                m.asset.asset_code if m.asset else "-",
                m.asset.get_category_display() if m.asset else "-",
                m.location_display,
                m.recipient_name,
                m.get_allocated_to_type_display(),
                m.handover_phone or "-",
                f"{m.quantity} {m.asset.unit if m.asset else 'Pcs'}",
                m.issue_date.strftime('%Y-%m-%d') if m.issue_date else '-',
                m.expected_return_date.strftime('%Y-%m-%d') if m.expected_return_date else '-',
                m.get_status_display(),
                m.remarks or "-"
            ])

        style_worksheet(ws, headers, alignments)

    # 7. Site Transfers
    def populate_site_transfers(ws):
        headers = [
            "GATE PASS #", "DESTINATION SITE", "VEHICLE NUMBER", "DRIVER / CONTACT",
            "RECIPIENT OFFICER", "ASSET NAME", "ASSET CODE", "QUANTITY DISPATCHED",
            "DISPATCH DATE", "EXPECTED RETURN", "STATUS", "DISPATCH REMARKS"
        ]
        ws.append(headers)
        alignments = [
            center_align, left_align, center_align, center_align,
            left_align, left_align, center_align, center_align,
            center_align, center_align, center_align, left_align
        ]

        qs = MessAssetAllocation.objects.filter(allocated_to_type='OTHER_SITE').select_related('asset').all().order_by('-created_at')
        if selected_ids and module == 'site_transfers':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(location_name__icontains=q) | Q(handover_to__icontains=q) | Q(vehicle_number__icontains=q) | Q(gate_pass_no__icontains=q))

        for t in qs:
            ws.append([
                t.gate_pass_no or f"GP-{t.id:04d}",
                t.location_name or "External Project Site",
                t.vehicle_number or "-",
                t.handover_phone or "-",
                t.handover_to or "-",
                t.asset.name if t.asset else "-",
                t.asset.asset_code if t.asset else "-",
                f"{t.quantity} {t.asset.unit if t.asset else 'Pcs'}",
                t.issue_date.strftime('%Y-%m-%d') if t.issue_date else '-',
                t.expected_return_date.strftime('%Y-%m-%d') if t.expected_return_date else '-',
                t.get_status_display(),
                t.remarks or "-"
            ])

        style_worksheet(ws, headers, alignments)

    # 8. Mess Meals
    def populate_mess_meals(ws):
        headers = [
            "DATE", "PUNCH TIME", "EMPLOYEE ID", "WORKER NAME",
            "CONTRACTOR / AGENCY", "MESS LOCATION", "MEAL WINDOW",
            "STATUS", "ENTRY MODE", "REMARKS"
        ]
        ws.append(headers)
        alignments = [
            center_align, center_align, center_align, left_align,
            left_align, left_align, center_align,
            center_align, center_align, left_align
        ]

        qs = MessLog.objects.select_related('employee', 'mess_location').all().order_by('-punch_time')
        if selected_ids and module == 'mess_meals':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(mess_location__name__icontains=q))

        for l in qs[:10000]:
            ws.append([
                l.date.strftime('%Y-%m-%d') if l.date else '-',
                l.punch_time.strftime('%H:%M:%S') if l.punch_time else '-',
                l.employee.emp_id or f"EMP-{l.employee.id}",
                l.employee.name,
                l.employee.contractor_agency or "Company Direct",
                l.mess_location.name if l.mess_location else "Main Canteen",
                l.get_meal_type_display(),
                l.status,
                l.entry_mode or "SCANNER",
                l.remarks or "-"
            ])

        style_worksheet(ws, headers, alignments)

    # 9. Mess Wastage
    def populate_mess_wastage(ws):
        headers = [
            "DATE", "MEAL TYPE", "MESS FACILITY", "PREPARED (KG)",
            "WASTED (KG)", "WASTAGE %",
            "CATERER / CONTRACTOR", "REMARKS / REASONS"
        ]
        ws.append(headers)
        alignments = [
            center_align, center_align, left_align, center_align,
            center_align, center_align,
            left_align, left_align
        ]

        qs = MessWastageLog.objects.select_related('mess_location').all().order_by('-date')
        if selected_ids and module == 'mess_wastage':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if q: qs = qs.filter(Q(contractor_name__icontains=q) | Q(remarks__icontains=q) | Q(mess_location__name__icontains=q))

        for w in qs:
            pct = f"{round((w.wasted_qty_kg / w.prepared_qty_kg * 100), 1)}%" if w.prepared_qty_kg > 0 else "0%"
            ws.append([
                w.date.strftime('%Y-%m-%d') if w.date else '-',
                w.meal_type,
                w.mess_location.name if w.mess_location else "Central Mess",
                f"{w.prepared_qty_kg} kg",
                f"{w.wasted_qty_kg} kg",
                pct,
                w.contractor_name or "-",
                w.remarks or "-"
            ])

        style_worksheet(ws, headers, alignments)

    # 10. Mess Feedback
    def populate_mess_feedback(ws):
        headers = [
            "DATE", "MEAL WINDOW", "WORKER NAME", "EMPLOYEE ID",
            "AGENCY", "RATING / FEEDBACK TAG", "COMMENTS & SUGGESTIONS"
        ]
        ws.append(headers)
        alignments = [
            center_align, center_align, left_align, center_align,
            left_align, center_align, left_align
        ]

        qs = MessFeedback.objects.select_related('employee').all().order_by('-created_at')
        if selected_ids and module == 'mess_feedback':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(comments__icontains=q))

        for fb in qs:
            ws.append([
                fb.date.strftime('%Y-%m-%d') if fb.date else '-',
                fb.get_meal_type_display(),
                fb.employee.name if fb.employee else "Anonymous Worker",
                fb.employee.emp_id or (f"EMP-{fb.employee.id}" if fb.employee else "-"),
                fb.employee.contractor_agency or "Company" if fb.employee else "-",
                fb.get_feedback_tag_display(),
                fb.comments or "-"
            ])

        style_worksheet(ws, headers, alignments)

    # 11. Board Update (Official Template Fallback)
    if module == 'board_update' and not selected_sections:
        template_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'board update.xlsx')
        if os.path.exists(template_path):
            wb_board = openpyxl.load_workbook(template_path)
            ws_board = wb_board['Board Update'] if 'Board Update' in wb_board.sheetnames else wb_board.active
            target_date = from_date or timezone.now().strftime('%Y-%m-%d')
            ws_board['J1'] = target_date
            filename = f"Board_Update_{target_date}.xlsx"
            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            wb_board.save(response)
            return response

    # Dispatch: Support Multi-Section Checkboxes (sections parameter)
    if selected_sections:
        if 'block_summary' in selected_sections and include_block_summary:
            populate_block_summary(get_or_create_sheet("Block Master Summary"))
        if 'room_detail' in selected_sections:
            populate_room_detail(get_or_create_sheet("Room Kundali Register"))
        if 'rooms' in selected_sections:
            populate_rooms(get_or_create_sheet("Rooms & Occupancy"))
        if 'leaves' in selected_sections:
            populate_leaves(get_or_create_sheet("Leave Records"))
        if 'resident_assets' in selected_sections:
            populate_resident_assets(get_or_create_sheet("Resident Assets"))
        if 'mess_assets' in selected_sections:
            populate_mess_assets(get_or_create_sheet("Mess & Kitchen Assets"))
        if 'site_transfers' in selected_sections:
            populate_site_transfers(get_or_create_sheet("Site Transfers"))
        if 'mess_meals' in selected_sections:
            populate_mess_meals(get_or_create_sheet("Mess Meals"))
        if 'mess_wastage' in selected_sections:
            populate_mess_wastage(get_or_create_sheet("Mess Wastage"))
        if 'mess_feedback' in selected_sections:
            populate_mess_feedback(get_or_create_sheet("Mess Feedback"))
        filename = f"camp_custom_sections{'_'+block_clean if block_clean else ''}.xlsx"
    elif module == 'block_summary':
        if include_block_summary:
            populate_block_summary(get_or_create_sheet("Block Master Summary"))
        populate_room_detail(get_or_create_sheet(f"Block {block_clean or 'All'} Occupants"))
        filename = f"camp_block_{block_clean or 'master'}_summary_and_occupants.xlsx" if include_block_summary else f"camp_block_{block_clean or 'all'}_occupants.xlsx"
    elif module == 'room_detail':
        populate_room_detail(get_or_create_sheet("Room Kundali Register"))
        filename = f"camp_room_wise_register{'_'+(room_filter or block_clean) if (room_filter or block_clean) else ''}.xlsx"
    elif module == 'rooms':
        populate_rooms(get_or_create_sheet("Rooms & Occupancy"))
        filename = "camp_rooms_occupancy_report.xlsx"
    elif module == 'leaves':
        populate_leaves(get_or_create_sheet("Leave Records"))
        filename = "camp_employee_leave_records.xlsx"
    elif module == 'resident_assets':
        populate_resident_assets(get_or_create_sheet("Resident Assets"))
        filename = "camp_resident_asset_allotments.xlsx"
    elif module == 'mess_assets':
        populate_mess_assets(get_or_create_sheet("Mess & Kitchen Assets"))
        filename = "camp_mess_kitchen_assets.xlsx"
    elif module == 'site_transfers':
        populate_site_transfers(get_or_create_sheet("Site Transfers"))
        filename = "camp_external_site_transfers.xlsx"
    elif module == 'mess_meals':
        populate_mess_meals(get_or_create_sheet("Mess Consumption Logs"))
        filename = "mess_meal_consumption_report.xlsx"
    elif module == 'mess_wastage':
        populate_mess_wastage(get_or_create_sheet("Food Wastage Register"))
        filename = "mess_food_wastage_report.xlsx"
    elif module == 'mess_feedback':
        populate_mess_feedback(get_or_create_sheet("Mess Food Feedback"))
        filename = "mess_feedback_ratings_report.xlsx"
    else:
        if include_block_summary:
            populate_block_summary(get_or_create_sheet("Block Master Summary"))
        populate_room_detail(get_or_create_sheet("Room Kundali Register"))
        populate_rooms(get_or_create_sheet("Rooms & Occupancy"))
        populate_leaves(get_or_create_sheet("Leave Records"))
        populate_resident_assets(get_or_create_sheet("Resident Assets"))
        populate_mess_assets(get_or_create_sheet("Mess & Kitchen Assets"))
        populate_site_transfers(get_or_create_sheet("Site Transfers"))
        populate_mess_meals(get_or_create_sheet("Mess Meals"))
        populate_mess_wastage(get_or_create_sheet("Mess Wastage"))
        populate_mess_feedback(get_or_create_sheet("Mess Feedback"))
        filename = "camp_master_consolidated_export.xlsx"

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
def export_camp_universal_pdf(request):
    """
    Universal Printable Landscape PDF Report Engine for Camp & Mess Management System.
    Generates high-resolution, print-optimized landscape reports for:
    Block Summary, Room Kundali Register, Rooms, Leaves, Resident Assets, Mess Assets,
    Site Transfers, Mess Consumption, Food Wastage, Feedback, Board Update, and Master Consolidated.
    Supports multi-section checkbox selection (sections parameter), natural numerical room sorting,
    and granular row filtering via selected_ids.
    """
    from django.utils import timezone
    from django.db.models import Q
    from django.http import HttpResponse
    from collections import defaultdict, Counter
    from portal.models import (
        CampRoom, CampBlock, Employee, EmployeeLeaveRecord,
        CampAssetAllotment, MessAssetItem, MessAssetAllocation,
        MessLog, MessWastageLog, MessFeedback, MessMenu, MessMealWindow
    )

    module = request.GET.get('module', 'all_master').strip().lower()
    sections_str = request.GET.get('sections', '').strip()
    selected_sections = [s.strip().lower() for s in sections_str.split(',') if s.strip()] if sections_str else []

    from_date = request.GET.get('from_date', '').strip()
    to_date = request.GET.get('to_date', '').strip()
    agency = request.GET.get('agency', '').strip()
    block_filter = request.GET.get('block', '').strip()
    room_filter = request.GET.get('room', '').strip()
    status_filter = request.GET.get('status', '').strip()
    q = request.GET.get('q', '').strip()
    selected_ids_str = request.GET.get('selected_ids', '').strip()
    selected_ids = [s.strip() for s in selected_ids_str.split(',') if s.strip()] if selected_ids_str else None

    block_clean = block_filter.replace('Block ', '').strip() if block_filter else ''
    cols_str = request.GET.get('cols', '').strip()
    generated_time = timezone.now().strftime('%d %b %Y, %I:%M %p')

    include_block_summary = request.GET.get('include_block_summary', '1').strip() != '0'

    def is_section_active(sec_key, default_modules):
        if not include_block_summary and sec_key == 'block_summary':
            return False
        if selected_sections:
            return sec_key in selected_sections
        return module in default_modules

    excel_url = f"/camp/export/universal/?module={module}&include_block_summary={1 if include_block_summary else 0}&from_date={from_date}&to_date={to_date}&agency={agency}&block={block_filter}&room={room_filter}&status={status_filter}&q={q}"
    if selected_sections:
        excel_url += f"&sections={sections_str}"
    if selected_ids_str:
        excel_url += f"&selected_ids={selected_ids_str}"
    if cols_str:
        excel_url += f"&cols={cols_str}"

    sections_html = []

    # 1. Block-Wise Summary
    if is_section_active('block_summary', ['block_summary', 'all_master']):
        b_qs = CampBlock.objects.prefetch_related('rooms').all().order_by('block_code')
        if selected_ids and module == 'block_summary':
            b_qs = b_qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if block_filter:
            b_qs = b_qs.filter(Q(block_code__icontains=block_filter) | Q(name__icontains=block_filter) | Q(block_code__icontains=block_clean))
        if q:
            b_qs = b_qs.filter(Q(block_code__icontains=q) | Q(name__icontains=q) | Q(category__icontains=q))

        rows = []
        for b in b_qs:
            b_rooms = sorted(b.rooms.all(), key=lambda r: _natural_sort_key(r.room_number))
            r_nums = set(r.room_number.strip().upper() for r in b_rooms)
            b_letter = b.block_code.replace('Block ', '').strip()
            emp_qs = Employee.objects.filter(status='Active').filter(
                Q(camp_room__in=r_nums) | Q(camp_room__istartswith=b_letter)
            )
            if agency and agency != 'ALL': emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
            if room_filter: emp_qs = emp_qs.filter(camp_room__icontains=room_filter)

            tot_rooms = len(b_rooms)
            cap = sum(r.capacity for r in b_rooms) or b.capacity or 0
            occ = emp_qs.count()
            vac = max(0, cap - occ)
            occ_pct = f"{round(occ / cap * 100)}%" if cap > 0 else "0%"
            inside_cnt = emp_qs.exclude(shift_remarks__icontains='leave').exclude(status='On Leave').count()

            fans = sum(r.fan_count for r in b_rooms)
            lights = sum(r.tubelight_count for r in b_rooms)

            desigs = [e.designation.strip() for e in emp_qs if e.designation]
            trade_counter = Counter(desigs)
            top_trades = [f"{t} ({cnt})" for t, cnt in trade_counter.most_common(2)]
            top_trades_str = ", ".join(top_trades) if top_trades else "Vacant"

            rows.append(f"""
            <tr>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{b.block_code}</td>
                <td style="font-weight:bold;">{b.name or f"Block {b.block_code}"}</td>
                <td style="text-align:center;">{b.get_category_display() if hasattr(b, 'get_category_display') else (b.category or 'General')}</td>
                <td style="text-align:center;">{tot_rooms}</td>
                <td style="text-align:center;">{cap}</td>
                <td style="text-align:center; font-weight:bold; color:#16a34a;">{occ}</td>
                <td style="text-align:center; color:#d97706;">{vac}</td>
                <td style="text-align:center; font-weight:bold;">{occ_pct}</td>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{inside_cnt}</td>
                <td style="text-align:center;">{fans} Fans | {lights} Lights</td>
                <td>{top_trades_str}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section">
            <h3>🏢 Camp Block Master Architecture &amp; Summary ({len(rows)} Blocks)</h3>
            <table class="table-block-summary">
                <thead>
                    <tr>
                        <th>Block Code</th>
                        <th>Block Name</th>
                        <th>Category</th>
                        <th>Rooms</th>
                        <th>Capacity</th>
                        <th>Occupants</th>
                        <th>Vacant</th>
                        <th>Occ %</th>
                        <th>Inside</th>
                        <th>Fans / Lights</th>
                        <th>Top Trades Distribution</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="11" style="text-align:center; padding:16px; color:#64748b;">No blocks found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 2. Room-Wise Detail Kundali (Naturally Sorted)
    if is_section_active('room_detail', ['block_summary', 'room_detail', 'all_master']):
        r_qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
        if block_filter:
            r_qs = r_qs.filter(Q(block__block_code__icontains=block_filter) | Q(block__name__icontains=block_filter) | Q(room_number__istartswith=block_clean))
        if room_filter:
            r_qs = r_qs.filter(room_number__icontains=room_filter)
        if q:
            r_qs = r_qs.filter(Q(room_number__icontains=q) | Q(block__block_code__icontains=q) | Q(block__block_name__icontains=q))

        r_list = sorted(list(r_qs), key=lambda r: _natural_sort_key(r.room_number))

        allotments = CampAssetAllotment.objects.select_related('employee', 'room').filter(room__in=r_list)
        if agency and agency != 'ALL': allotments = allotments.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL': allotments = allotments.filter(condition=status_filter)

        allotments_by_room = defaultdict(list)
        for a in allotments:
            if a.room_id: allotments_by_room[a.room_id].append(a)

        emp_qs = Employee.objects.filter(status='Active')
        if agency and agency != 'ALL': emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
        emp_by_room = defaultdict(list)
        for e in emp_qs:
            if e.camp_room: emp_by_room[e.camp_room.strip().upper()].append(e)

        rows = []
        for r in r_list:
            r_allots = allotments_by_room.get(r.id, [])
            r_allots.sort(key=lambda a: _natural_sort_key(a.bed_number or ''))
            room_emps = emp_by_room.get(r.room_number.strip().upper(), [])
            block_name = r.block.block_code if r.block else 'Camp'
            key_status_disp = r.get_key_status_display()

            if r_allots:
                for a in r_allots:
                    e = a.employee
                    if not e: continue
                    if selected_ids and module in ['room_detail', 'block_summary']:
                        if f"A_{a.id}" not in selected_ids and str(a.id) not in selected_ids:
                            continue
                    cot_mat = f"{'Cot: Yes' if a.bed_cot_allotted else 'Cot: No'} | {'Mat: Yes' if a.mattress_allotted else 'Mat: No'}"
                    rows.append(f"""
                    <tr>
                        <td style="text-align:center; font-weight:bold;">{block_name}</td>
                        <td style="text-align:center; font-weight:bold; color:#0284c7;">{r.room_number}</td>
                        <td style="text-align:center;">{a.bed_number or 'Bed 1'}</td>
                        <td style="font-weight:bold;">{e.name}</td>
                        <td style="text-align:center; color:#64748b;">{e.emp_id or f"EMP-{e.id}"}</td>
                        <td>{e.designation or 'Worker'}</td>
                        <td>{e.contractor_agency or 'Company Direct'}</td>
                        <td style="text-align:center;">{key_status_disp}</td>
                        <td style="text-align:center;">{cot_mat}</td>
                        <td style="text-align:center; font-weight:bold;">{a.get_condition_display()}</td>
                    </tr>
                    """)
            elif room_emps:
                for idx, e in enumerate(room_emps, 1):
                    if selected_ids and module in ['room_detail', 'block_summary']:
                        if f"E_{e.id}" not in selected_ids and str(e.id) not in selected_ids:
                            continue
                    rows.append(f"""
                    <tr>
                        <td style="text-align:center; font-weight:bold;">{block_name}</td>
                        <td style="text-align:center; font-weight:bold; color:#0284c7;">{r.room_number}</td>
                        <td style="text-align:center;">Bed {idx}</td>
                        <td style="font-weight:bold;">{e.name}</td>
                        <td style="text-align:center; color:#64748b;">{e.emp_id or f"EMP-{e.id}"}</td>
                        <td>{e.designation or 'Worker'}</td>
                        <td>{e.contractor_agency or 'Company Direct'}</td>
                        <td style="text-align:center;">{key_status_disp}</td>
                        <td style="text-align:center;">Standard Issue</td>
                        <td style="text-align:center; font-weight:bold; color:#16a34a;">Active</td>
                    </tr>
                    """)
            else:
                if not agency or agency == 'ALL':
                    if selected_ids and module in ['room_detail', 'block_summary']:
                        if f"R_{r.id}" not in selected_ids and str(r.id) not in selected_ids:
                            continue
                    rows.append(f"""
                    <tr>
                        <td style="text-align:center; font-weight:bold;">{block_name}</td>
                        <td style="text-align:center; font-weight:bold; color:#0284c7;">{r.room_number}</td>
                        <td style="text-align:center; color:#64748b;">0 / {r.capacity} Beds</td>
                        <td colspan="4" style="text-align:center; color:#94a3b8; font-style:italic;">(Vacant Room - Available for Allotment)</td>
                        <td style="text-align:center;">{key_status_disp}</td>
                        <td style="text-align:center;">Fans: {r.fan_count}</td>
                        <td style="text-align:center; color:#64748b;">{r.get_door_lock_status_display()}</td>
                    </tr>
                    """)

        block_title_prefix = f"Block {block_clean} - " if block_clean else ""
        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🚪 {block_title_prefix}Detailed Room &amp; Bed Occupant Kundali ({len(rows)} Records)</h3>
            <table class="table-room-detail">
                <thead>
                    <tr>
                        <th>Block</th>
                        <th>Room #</th>
                        <th>Bed #</th>
                        <th>Resident Name</th>
                        <th>Emp ID</th>
                        <th>Trade</th>
                        <th>Agency</th>
                        <th>Key Status</th>
                        <th>Assets / Bedding</th>
                        <th>Condition</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="10" style="text-align:center; padding:16px; color:#64748b;">No room records found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 3. Rooms & Occupancy Summary (Naturally Sorted)
    if is_section_active('rooms', ['rooms', 'all_master']):
        qs = CampRoom.objects.select_related('block', 'key_issued_to').all()
        if selected_ids and module == 'rooms':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if block_filter: qs = qs.filter(Q(block__block_code__icontains=block_filter) | Q(block__name__icontains=block_filter) | Q(room_number__istartswith=block_clean))
        if room_filter: qs = qs.filter(room_number__icontains=room_filter)
        if q: qs = qs.filter(Q(room_number__icontains=q) | Q(block__block_name__icontains=q) | Q(block__block_code__icontains=q))
        
        r_list = sorted(list(qs), key=lambda r: _natural_sort_key(r.room_number))

        emp_qs = Employee.objects.filter(status='Active')
        if agency and agency != 'ALL': emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
        residents_by_room = defaultdict(list)
        for emp in emp_qs:
            if emp.camp_room:
                residents_by_room[emp.camp_room.strip().upper()].append(emp)

        rows = []
        for r in r_list:
            res_list = residents_by_room.get(r.room_number.strip().upper(), [])
            occ = len(res_list)
            vac = max(0, r.capacity - occ)
            pct = f"{round((occ / r.capacity * 100))}%" if r.capacity > 0 else "0%"
            if agency and agency != 'ALL' and occ == 0: continue
            res_str = ", ".join([f"{e.name} ({e.emp_id})" for e in res_list[:3]]) + (f" +{len(res_list)-3} more" if len(res_list) > 3 else "")
            rows.append(f"""
            <tr>
                <td style="text-align:center; font-weight:bold;">{r.block.block_code if r.block else '-'}</td>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{r.room_number}</td>
                <td style="text-align:center;">{r.capacity}</td>
                <td style="text-align:center; font-weight:bold; color:#16a34a;">{occ}</td>
                <td style="text-align:center; color:#d97706;">{vac}</td>
                <td style="text-align:center;">{pct}</td>
                <td style="text-align:center;">{r.get_key_status_display()}</td>
                <td style="text-align:center;">{r.fan_count} ({r.get_fan_status_display()})</td>
                <td style="text-align:center;">{r.tubelight_count} ({r.get_tubelight_status_display()})</td>
                <td style="text-align:center;">{r.get_door_lock_status_display()}</td>
                <td>{res_str or 'Vacant'}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🏢 Camp Rooms &amp; Occupancy Register ({len(rows)} Rooms)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Block</th>
                        <th>Room #</th>
                        <th>Beds</th>
                        <th>Occupants</th>
                        <th>Vacant</th>
                        <th>Occ %</th>
                        <th>Key Status</th>
                        <th>Fans</th>
                        <th>Lights</th>
                        <th>Lock</th>
                        <th>Active Residents Roster</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="11" style="text-align:center; padding:16px; color:#64748b;">No rooms found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 4. Leaves
    if is_section_active('leaves', ['leaves', 'all_master']):
        rows = []
        if EmployeeLeaveRecord.objects.exists():
            qs = EmployeeLeaveRecord.objects.select_related('employee', 'created_by').all().order_by('-start_date')
            if selected_ids and module == 'leaves':
                qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
            if from_date: qs = qs.filter(end_date__gte=from_date)
            if to_date: qs = qs.filter(start_date__lte=to_date)
            if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
            if block_filter: qs = qs.filter(employee__camp_room__icontains=block_clean)
            if room_filter: qs = qs.filter(employee__camp_room__icontains=room_filter)
            if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
            if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(form_number__icontains=q))

            for lv in qs[:500]:
                rows.append(f"""
                <tr>
                    <td style="text-align:center; font-weight:bold; color:#dc2626;">{lv.form_number or 'LV-'+str(lv.id)}</td>
                    <td style="text-align:center;">{lv.get_leave_type_display()}</td>
                    <td style="font-weight:bold;">{lv.employee.name} <span style="color:#64748b; font-weight:normal;">({lv.employee.emp_id})</span></td>
                    <td>{lv.employee.contractor_agency or 'Company'}</td>
                    <td style="text-align:center;">{lv.start_date.strftime('%Y-%m-%d') if lv.start_date else '-'}</td>
                    <td style="text-align:center;">{lv.end_date.strftime('%Y-%m-%d') if lv.end_date else '-'}</td>
                    <td style="text-align:center; font-weight:bold;">{lv.total_days}d</td>
                    <td>{lv.destination or '-'}</td>
                    <td style="text-align:center;">{lv.contact_number or '-'}</td>
                    <td style="text-align:center; font-weight:bold;">{lv.get_status_display()}</td>
                    <td style="text-align:center;">{lv.approved_by or '-'}</td>
                </tr>
                """)
        else:
            emp_qs = Employee.objects.filter(Q(status='On Leave') | Q(shift_remarks__icontains='leave')).order_by('name')
            if selected_ids and module == 'leaves':
                emp_qs = emp_qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
            if agency and agency != 'ALL': emp_qs = emp_qs.filter(contractor_agency__icontains=agency)
            if block_filter: emp_qs = emp_qs.filter(camp_room__icontains=block_clean)
            if room_filter: emp_qs = emp_qs.filter(camp_room__icontains=room_filter)
            if q: emp_qs = emp_qs.filter(Q(name__icontains=q) | Q(emp_id__icontains=q) | Q(shift_remarks__icontains=q))

            for e in emp_qs[:500]:
                rows.append(f"""
                <tr>
                    <td style="text-align:center; font-weight:bold; color:#dc2626;">LV-EMP-{e.id}</td>
                    <td style="text-align:center;">Authorized Leave</td>
                    <td style="font-weight:bold;">{e.name} <span style="color:#64748b; font-weight:normal;">({e.emp_id})</span></td>
                    <td>{e.contractor_agency or 'Company'}</td>
                    <td style="text-align:center;">{from_date or '-'}</td>
                    <td style="text-align:center;">{to_date or '-'}</td>
                    <td style="text-align:center; font-weight:bold;">-</td>
                    <td>{e.camp_room or 'Outside'}</td>
                    <td style="text-align:center;">{e.contact_info or '-'}</td>
                    <td style="text-align:center; font-weight:bold;">{e.status}</td>
                    <td style="text-align:center;">{e.shift_remarks or 'HR Approved'}</td>
                </tr>
                """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>📋 Camp Workforce Leave Register ({len(rows)} Records)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Form #</th>
                        <th>Type</th>
                        <th>Worker Name</th>
                        <th>Agency</th>
                        <th>Start Date</th>
                        <th>End Date</th>
                        <th>Days</th>
                        <th>Destination</th>
                        <th>Phone</th>
                        <th>Status</th>
                        <th>Approved By</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="11" style="text-align:center; padding:16px; color:#64748b;">No leave records found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 5. Resident Assets (Naturally Sorted)
    if is_section_active('resident_assets', ['resident_assets', 'all_master']):
        qs = CampAssetAllotment.objects.select_related('employee', 'room').all()
        if selected_ids and module == 'resident_assets':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if block_filter: qs = qs.filter(Q(room__block__block_code__icontains=block_filter) | Q(employee__camp_room__istartswith=block_clean))
        if room_filter: qs = qs.filter(Q(room__room_number__icontains=room_filter) | Q(employee__camp_room__icontains=room_filter))
        if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL': qs = qs.filter(condition=status_filter)
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(room__room_number__icontains=q))

        a_list = sorted(list(qs), key=lambda a: (_natural_sort_key(a.room.room_number if a.room else (a.employee.camp_room if a.employee else '')), _natural_sort_key(a.bed_number or '')))

        rows = []
        for a in a_list[:500]:
            r_no = a.room.room_number if a.room else (a.employee.camp_room or '-')
            rows.append(f"""
            <tr>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{r_no}</td>
                <td style="text-align:center;">{a.bed_number or 'Bed 1'}</td>
                <td style="font-weight:bold;">{a.employee.name}</td>
                <td style="text-align:center; color:#64748b;">{a.employee.emp_id or f"EMP-{a.employee.id}"}</td>
                <td>{a.employee.contractor_agency or 'Company'}</td>
                <td style="text-align:center;">{'✓' if a.bed_cot_allotted else '✗'}</td>
                <td style="text-align:center;">{'✓' if a.mattress_allotted else '✗'}</td>
                <td style="text-align:center;">{'✓' if a.pillow_allotted else '✗'}</td>
                <td style="text-align:center;">{'✓' if a.fan_allotted else '✗'}</td>
                <td style="text-align:center; font-weight:bold;">{a.get_condition_display()}</td>
                <td style="text-align:center;">{a.issue_date.strftime('%Y-%m-%d') if a.issue_date else '-'}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🛋️ Camp Resident Bed &amp; Asset Allotments ({len(rows)} Records)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Room #</th>
                        <th>Bed #</th>
                        <th>Worker Name</th>
                        <th>Emp ID</th>
                        <th>Agency</th>
                        <th>Cot</th>
                        <th>Mat</th>
                        <th>Pil</th>
                        <th>Fan</th>
                        <th>Condition</th>
                        <th>Issue Date</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="11" style="text-align:center; padding:16px; color:#64748b;">No resident asset allotments found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 6. Mess Assets
    if is_section_active('mess_assets', ['mess_assets', 'all_master']):
        qs = MessAssetAllocation.objects.exclude(allocated_to_type='OTHER_SITE').select_related('asset', 'staff_member', 'mess_location').all().order_by('-created_at')
        if selected_ids and module == 'mess_assets':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(asset__name__icontains=q) | Q(asset__asset_code__icontains=q) | Q(staff_name__icontains=q) | Q(location_name__icontains=q))

        rows = []
        for m in qs[:300]:
            rows.append(f"""
            <tr>
                <td style="font-weight:bold;">{m.asset.name if m.asset else '-'}</td>
                <td style="text-align:center; color:#0284c7;">{m.asset.asset_code if m.asset else '-'}</td>
                <td style="text-align:center;">{m.asset.get_category_display() if m.asset else '-'}</td>
                <td>{m.location_display}</td>
                <td>{m.recipient_name}</td>
                <td style="text-align:center; font-weight:bold;">{m.quantity} {m.asset.unit if m.asset else 'Pcs'}</td>
                <td style="text-align:center;">{m.issue_date.strftime('%Y-%m-%d') if m.issue_date else '-'}</td>
                <td style="text-align:center; font-weight:bold;">{m.get_status_display()}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🍳 Mess &amp; Kitchen Asset Inventory ({len(rows)} Items)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Asset Name</th>
                        <th>Asset Code</th>
                        <th>Category</th>
                        <th>Facility / Location</th>
                        <th>Recipient</th>
                        <th>Quantity</th>
                        <th>Issue Date</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="8" style="text-align:center; padding:16px; color:#64748b;">No mess assets found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 7. Site Transfers
    if is_section_active('site_transfers', ['site_transfers', 'all_master']):
        qs = MessAssetAllocation.objects.filter(allocated_to_type='OTHER_SITE').select_related('asset').all().order_by('-created_at')
        if selected_ids and module == 'site_transfers':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(issue_date__gte=from_date)
        if to_date: qs = qs.filter(issue_date__lte=to_date)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(location_name__icontains=q) | Q(handover_to__icontains=q) | Q(vehicle_number__icontains=q) | Q(gate_pass_no__icontains=q))

        rows = []
        for t in qs[:300]:
            rows.append(f"""
            <tr>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{t.gate_pass_no or 'GP-'+str(t.id)}</td>
                <td style="font-weight:bold;">{t.location_name or 'External Site'}</td>
                <td style="text-align:center;">{t.vehicle_number or '-'}</td>
                <td>{t.handover_to or '-'}</td>
                <td>{t.asset.name if t.asset else '-'}</td>
                <td style="text-align:center; font-weight:bold;">{t.quantity} {t.asset.unit if t.asset else 'Pcs'}</td>
                <td style="text-align:center;">{t.issue_date.strftime('%Y-%m-%d') if t.issue_date else '-'}</td>
                <td style="text-align:center; font-weight:bold;">{t.get_status_display()}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🚚 External Project Site Transfers &amp; Dispatches ({len(rows)} Records)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Gate Pass #</th>
                        <th>Destination Site</th>
                        <th>Vehicle #</th>
                        <th>Recipient</th>
                        <th>Asset Name</th>
                        <th>Qty</th>
                        <th>Date</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="8" style="text-align:center; padding:16px; color:#64748b;">No external transfers found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 8. Mess Meals
    if is_section_active('mess_meals', ['mess_meals', 'all_master']):
        qs = MessLog.objects.select_related('employee', 'mess_location').all().order_by('-punch_time')
        if selected_ids and module == 'mess_meals':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if agency and agency != 'ALL': qs = qs.filter(employee__contractor_agency__icontains=agency)
        if status_filter and status_filter != 'ALL': qs = qs.filter(status=status_filter)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(employee__emp_id__icontains=q) | Q(mess_location__name__icontains=q))

        rows = []
        for l in qs[:500]:
            rows.append(f"""
            <tr>
                <td style="text-align:center;">{l.date.strftime('%Y-%m-%d') if l.date else '-'}</td>
                <td style="text-align:center;">{l.punch_time.strftime('%H:%M') if l.punch_time else '-'}</td>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{l.employee.emp_id or f"EMP-{l.employee.id}"}</td>
                <td style="font-weight:bold;">{l.employee.name}</td>
                <td>{l.employee.contractor_agency or 'Company'}</td>
                <td>{l.mess_location.name if l.mess_location else 'Main Canteen'}</td>
                <td style="text-align:center; font-weight:bold; color:#ea580c;">{l.get_meal_type_display()}</td>
                <td style="text-align:center;">{l.status}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🍱 Mess Meal Consumption Logs ({len(rows)} Records)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Time</th>
                        <th>Emp ID</th>
                        <th>Worker Name</th>
                        <th>Agency</th>
                        <th>Mess Location</th>
                        <th>Meal Window</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="8" style="text-align:center; padding:16px; color:#64748b;">No meal punches found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 9. Mess Wastage
    if is_section_active('mess_wastage', ['mess_wastage', 'all_master']):
        qs = MessWastageLog.objects.select_related('mess_location').all().order_by('-date')
        if selected_ids and module == 'mess_wastage':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if q: qs = qs.filter(Q(contractor_name__icontains=q) | Q(remarks__icontains=q) | Q(mess_location__name__icontains=q))

        rows = []
        for w in qs[:300]:
            pct = f"{round((w.wasted_qty_kg / w.prepared_qty_kg * 100), 1)}%" if w.prepared_qty_kg > 0 else "0%"
            rows.append(f"""
            <tr>
                <td style="text-align:center;">{w.date.strftime('%Y-%m-%d') if w.date else '-'}</td>
                <td style="text-align:center; font-weight:bold;">{w.meal_type}</td>
                <td>{w.mess_location.name if w.mess_location else 'Central Mess'}</td>
                <td style="text-align:center;">{w.prepared_qty_kg} kg</td>
                <td style="text-align:center; font-weight:bold; color:#dc2626;">{w.wasted_qty_kg} kg</td>
                <td style="text-align:center; font-weight:bold; color:#dc2626;">{pct}</td>
                <td>{w.contractor_name or w.remarks or '-'}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>🗑️ Food Wastage &amp; Leftover Register ({len(rows)} Records)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Meal</th>
                        <th>Mess Location</th>
                        <th>Prepared</th>
                        <th>Wasted</th>
                        <th>Wastage %</th>
                        <th>Caterer / Remarks</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="7" style="text-align:center; padding:16px; color:#64748b;">No wastage logs found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 10. Mess Feedback
    if is_section_active('mess_feedback', ['mess_feedback', 'all_master']):
        qs = MessFeedback.objects.select_related('employee').all().order_by('-created_at')
        if selected_ids and module == 'mess_feedback':
            qs = qs.filter(id__in=[int(x) for x in selected_ids if x.isdigit()])
        if from_date: qs = qs.filter(date__gte=from_date)
        if to_date: qs = qs.filter(date__lte=to_date)
        if q: qs = qs.filter(Q(employee__name__icontains=q) | Q(comments__icontains=q))

        rows = []
        for fb in qs[:300]:
            rows.append(f"""
            <tr>
                <td style="text-align:center;">{fb.date.strftime('%Y-%m-%d') if fb.date else '-'}</td>
                <td style="text-align:center;">{fb.get_meal_type_display()}</td>
                <td style="font-weight:bold;">{fb.employee.name if fb.employee else 'Anonymous'}</td>
                <td style="text-align:center; font-weight:bold; color:#0284c7;">{fb.get_feedback_tag_display()}</td>
                <td>{fb.comments or '-'}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>💬 Mess Food Quality &amp; Feedback Ratings ({len(rows)} Reviews)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Meal Type</th>
                        <th>Worker Name</th>
                        <th>Rating / Tag</th>
                        <th>Comments</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="5" style="text-align:center; padding:16px; color:#64748b;">No feedback records found.</td></tr>'}</tbody>
            </table>
        </div>
        """)

    # 11. Board Update
    if is_section_active('board_update', ['board_update']):
        blocks = 'ABCDEFGHIJKLMNOP'
        rows = []
        for b in blocks:
            if block_filter and b.upper() != block_clean.upper():
                continue
            emps = Employee.objects.filter(camp_room__istartswith=b, status='Active')
            tot = emps.count()
            nat = emps.filter(nationality__iexact='Bhutanese').count()
            exp = tot - nat
            rows.append(f"""
            <tr>
                <td style="text-align:center; font-weight:bold; font-size:12px; background:#f1f5f9;">Block {b}</td>
                <td style="text-align:center;">{nat if nat > 0 else '-'}</td>
                <td style="text-align:center; font-weight:bold;">{exp if exp > 0 else '-'}</td>
                <td style="text-align:center; font-weight:bold; color:#0284c7; background:#e0f2fe;">{tot}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Labour').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Gabin').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Painter').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Welder').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Mechanic').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Helper').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='HVD').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Scania').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='TM').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Tanker').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Excavator').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Supervisor').count() or '-'}</td>
                <td style="text-align:center;">{emps.filter(designation__icontains='Security').count() or '-'}</td>
            </tr>
            """)

        sections_html.append(f"""
        <div class="report-section" style="{'page-break-before: always;' if len(sections_html) > 0 else ''}">
            <h3>📋 Official Board Update - Block Architecture &amp; Key Trades (Blocks A to P)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Block</th>
                        <th>National</th>
                        <th>Expatriate</th>
                        <th>Total</th>
                        <th>Labour</th>
                        <th>Gabin</th>
                        <th>Painter</th>
                        <th>Welder</th>
                        <th>Mechanic</th>
                        <th>Helper</th>
                        <th>HVD</th>
                        <th>Scania</th>
                        <th>TM</th>
                        <th>Tanker</th>
                        <th>Excavator</th>
                        <th>Supervisor</th>
                        <th>Security</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """)

    body_content = "".join(sections_html)

    if selected_sections:
        module_display = ", ".join([s.replace('_', ' ').title() for s in selected_sections])
    else:
        module_display = module.replace('_', ' ').title()

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Camp &amp; Mess Management System - Official Report ({module_display})</title>
    <style>
        @page {{
            size: A4 landscape;
            margin: 10mm 12mm 10mm 12mm;
        }}
        @media print {{
            .no-print {{ display: none !important; }}
            body {{ background: white !important; margin: 0; padding: 0; }}
            .report-section {{ page-break-inside: avoid; }}
        }}
        body {{
            font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
            background: #f8fafc;
            color: #0f172a;
            margin: 0;
            padding: 16px;
            font-size: 13px;
            line-height: 1.5;
        }}
        .header-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #0284c7;
            padding-bottom: 8px;
            margin-bottom: 12px;
        }}
        .header-bar h1 {{
            margin: 0;
            font-size: 18px;
            font-weight: 800;
            color: #1e293b;
            letter-spacing: -0.02em;
        }}
        .header-bar p {{
            margin: 2px 0 0 0;
            font-size: 11px;
            color: #64748b;
        }}
        .btn-group {{
            display: flex;
            gap: 8px;
        }}
        .btn {{
            background: #0284c7;
            color: white;
            border: none;
            border-radius: 6px;
            padding: 6px 14px;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .btn:hover {{ background: #0369a1; }}
        .btn-excel {{
            background: #16a34a;
        }}
        .btn-excel:hover {{ background: #15803d; }}
        .filter-summary {{
            background: #f1f5f9;
            border-radius: 6px;
            padding: 8px 12px;
            margin-bottom: 14px;
            font-size: 11px;
            color: #475569;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
        }}
        .filter-summary span b {{
            color: #0f172a;
        }}
        .report-section {{
            margin-bottom: 24px;
            background: white;
            border-radius: 8px;
            border: 1px solid #cbd5e1;
            padding: 14px 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }}
        .report-section h3 {{
            margin: 0 0 10px 0;
            font-size: 14px;
            font-weight: 800;
            color: #1e293b;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 6px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 11px;
            line-height: 1.4;
        }}
        th {{
            background: #0f172a;
            color: #ffffff;
            padding: 7px 8px;
            font-weight: 800;
            font-size: 11px;
            text-align: left;
            border: 1px solid #334155;
            white-space: nowrap;
            letter-spacing: 0.02em;
        }}
        td {{
            padding: 6px 8px;
            border: 1px solid #cbd5e1;
            vertical-align: middle;
            font-size: 11px;
            color: #1e293b;
        }}
        tr:nth-child(even) {{
            background: #f8fafc;
        }}
        tr:hover {{
            background: #f1f5f9;
        }}
        /* Specialized column sizing for Block Architecture table */
        table.table-block-summary th:nth-child(1), table.table-block-summary td:nth-child(1) {{ width: 7%; }}
        table.table-block-summary th:nth-child(2), table.table-block-summary td:nth-child(2) {{ width: 12%; }}
        table.table-block-summary th:nth-child(3), table.table-block-summary td:nth-child(3) {{ width: 9%; }}
        table.table-block-summary th:nth-child(4), table.table-block-summary td:nth-child(4) {{ width: 6%; }}
        table.table-block-summary th:nth-child(5), table.table-block-summary td:nth-child(5) {{ width: 7%; }}
        table.table-block-summary th:nth-child(6), table.table-block-summary td:nth-child(6) {{ width: 7%; }}
        table.table-block-summary th:nth-child(7), table.table-block-summary td:nth-child(7) {{ width: 6%; }}
        table.table-block-summary th:nth-child(8), table.table-block-summary td:nth-child(8) {{ width: 6%; }}
        table.table-block-summary th:nth-child(9), table.table-block-summary td:nth-child(9) {{ width: 6%; }}
        table.table-block-summary th:nth-child(10), table.table-block-summary td:nth-child(10) {{ width: 12%; }}
        table.table-block-summary th:nth-child(11), table.table-block-summary td:nth-child(11) {{ width: 22%; }}

        /* Specialized column sizing for Room & Bed Occupant Kundali table */
        table.table-room-detail th:nth-child(1), table.table-room-detail td:nth-child(1) {{ width: 6%; }}
        table.table-room-detail th:nth-child(2), table.table-room-detail td:nth-child(2) {{ width: 7%; }}
        table.table-room-detail th:nth-child(3), table.table-room-detail td:nth-child(3) {{ width: 7%; }}
        table.table-room-detail th:nth-child(4), table.table-room-detail td:nth-child(4) {{ width: 17%; }}
        table.table-room-detail th:nth-child(5), table.table-room-detail td:nth-child(5) {{ width: 9%; }}
        table.table-room-detail th:nth-child(6), table.table-room-detail td:nth-child(6) {{ width: 12%; }}
        table.table-room-detail th:nth-child(7), table.table-room-detail td:nth-child(7) {{ width: 15%; }}
        table.table-room-detail th:nth-child(8), table.table-room-detail td:nth-child(8) {{ width: 9%; }}
        table.table-room-detail th:nth-child(9), table.table-room-detail td:nth-child(9) {{ width: 10%; }}
        table.table-room-detail th:nth-child(10), table.table-room-detail td:nth-child(10) {{ width: 8%; }}

        .footer {{
            border-top: 1px solid #cbd5e1;
            padding-top: 8px;
            margin-top: 16px;
            font-size: 10px;
            color: #64748b;
            display: flex;
            justify-content: space-between;
        }}
    </style>
</head>
<body>
    <div class="header-bar no-print">
        <div>
            <h1>📊 Camp &amp; Mess Universal Management Report</h1>
            <p>Generated: {generated_time} • Official Operations &amp; Facilities Documentation</p>
        </div>
        <div class="btn-group">
            <button onclick="window.print()" class="btn">🖨️ Print / Save as PDF</button>
            <a href="{excel_url}" class="btn btn-excel">📥 Download Excel (.xlsx)</a>
        </div>
    </div>

    <div class="filter-summary">
        <span><b>Report Modules:</b> {module_display}</span>
        <span><b>Block Filter:</b> {block_filter or 'All Blocks'}</span>
        <span><b>Room Filter:</b> {room_filter or 'All Rooms'}</span>
        <span><b>Agency Filter:</b> {agency or 'All Contractors'}</span>
        <span><b>Status Filter:</b> {status_filter or 'All Statuses'}</span>
        <span><b>Date Range:</b> {from_date or 'Any'} to {to_date or 'Present'}</span>
    </div>

    {body_content}

    <div class="footer">
        <div>Camp Management System • Security &amp; Facilities Administration</div>
        <div>System Generated Report • Landscape Orientation</div>
    </div>

    <script>
        window.addEventListener('DOMContentLoaded', () => {{
            setTimeout(() => {{
                window.print();
            }}, 600);
        }});
    </script>
</body>
</html>"""

    return HttpResponse(full_html)


# Safety Department Views & APIs
from .views_safety import *

