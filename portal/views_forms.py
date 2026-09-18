import json
import os
import datetime
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse, Http404, FileResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_protect
from django.utils import timezone
from django.db.models import Q, Count
from django.conf import settings

from portal.models import OfficeFormRecord

# Master Dictionary of all 8 Office Forms & Formats
FORM_TEMPLATES_CONFIG = {
    'fw_leave': {
        'key': 'fw_leave',
        'title': 'Foreign Worker Leave Form',
        'subtitle': '2026 New Version • Worker Permit & Zone Verification',
        'category': 'Leave & Attendance',
        'category_icon': '🌴',
        'icon': '✈️',
        'badge_color': '#3b82f6',
        'badge_text': '2026 Format',
        'template_file': 'Foreign Worker Leave_Form_2026 New.pdf',
        'description': 'Standard application form for foreign workers: includes Worker Permit No, Zone deployment, leave duration, reasons and supervisor approvals.',
        'fields': [
            {'name': 'worker_name', 'label': 'Name of Foreign Worker', 'type': 'text', 'required': True},
            {'name': 'labour_id', 'label': 'Labour ID / Worker ID', 'type': 'text', 'placeholder': 'e.g. HR-EMP-1042', 'required': True},
            {'name': 'designation', 'label': 'Designation at Site', 'type': 'text', 'required': True},
            {'name': 'permit_no', 'label': 'Work Permit No', 'type': 'text', 'required': True},
            {'name': 'contact_no', 'label': 'Contact Phone No', 'type': 'text'},
            {'name': 'zone', 'label': 'Work Zone', 'type': 'select', 'options': ['Zone One', 'Zone Two', 'Zone Three', 'Zone Four', 'Crusher', 'Workshop', 'Camp Area', 'Batching Plant']},
            {'name': 'leave_days', 'label': 'Leave Applied For (Number of Days)', 'type': 'number', 'required': True},
            {'name': 'from_date', 'label': 'From Date', 'type': 'date', 'required': True},
            {'name': 'to_date', 'label': 'To Date', 'type': 'date', 'required': True},
            {'name': 'leave_type', 'label': 'Leave Type', 'type': 'select', 'options': ['Casual Leave', 'Earn Leave', 'Medical Leave', 'Leave Without Pay (LWP)', 'Emergency Leave']},
            {'name': 'reason', 'label': 'Reason for Leave', 'type': 'textarea', 'required': True},
            {'name': 'supervisor_name', 'label': 'Recommended By (Supervisor)', 'type': 'text'},
            {'name': 'approver_name', 'label': 'Approved By (Project / HR Manager)', 'type': 'text'},
        ]
    },
    'rvjv_leave': {
        'key': 'rvjv_leave',
        'title': 'RVJV Staff Leave Form',
        'subtitle': 'Regular & Contract Staff Application',
        'category': 'Leave & Attendance',
        'category_icon': '🌴',
        'icon': '📝',
        'badge_color': '#8b5cf6',
        'badge_text': 'Staff Format',
        'template_file': 'RVJV leave form.pdf',
        'description': 'Comprehensive staff leave form covering Casual, Earned, Medical, Maternity, Paternity, Bereavement and LWP leaves with policy notes.',
        'fields': [
            {'name': 'applicant_name', 'label': 'Name of Applicant', 'type': 'text', 'required': True},
            {'name': 'emp_id', 'label': 'Employee ID', 'type': 'text', 'placeholder': 'e.g. HR-EMP-2041', 'required': True},
            {'name': 'designation', 'label': 'Designation at Site / Office', 'type': 'text', 'required': True},
            {'name': 'contact_no', 'label': 'Contact Phone No', 'type': 'text'},
            {'name': 'zone', 'label': 'Zone / Department', 'type': 'select', 'options': ['Zone 1 & 2', 'Zone 3 & 4', 'Office Area', 'Batching & Crusher', 'Workshop', 'Culvert & Structures']},
            {'name': 'leave_days', 'label': 'Leave Applied For (Number of Days)', 'type': 'number', 'required': True},
            {'name': 'from_date', 'label': 'From Date', 'type': 'date', 'required': True},
            {'name': 'to_date', 'label': 'To Date', 'type': 'date', 'required': True},
            {'name': 'leave_type', 'label': 'Leave Type', 'type': 'select', 'options': ['Casual Leave', 'Earn Leave', 'Medical Leave', 'Leave Without Pay (LWP)', 'Maternity Leave', 'Paternity Leave', 'Bereavement Leave']},
            {'name': 'reason', 'label': 'Detailed Reason', 'type': 'textarea', 'required': True},
            {'name': 'recommended_by', 'label': 'Leave Recommended By (HOD / Section Head)', 'type': 'text'},
            {'name': 'approved_by', 'label': 'Leave Approved By (Project Manager)', 'type': 'text'},
        ]
    },
    'no_dues': {
        'key': 'no_dues',
        'title': 'No Due Certificate (Clearance Form)',
        'subtitle': 'Departmental Asset & Liability Handover',
        'category': 'HR & Clearance',
        'category_icon': '📋',
        'icon': '🛡️',
        'badge_color': '#10b981',
        'badge_text': 'Exit Clearance',
        'template_file': 'No Dues Certificate.docx',
        'description': 'Mandatory clearance certificate certifying that the employee has completed all obligations and returned all assets to Store, OHS, Accounts, and Administration.',
        'fields': [
            {'name': 'employee_name', 'label': 'Employee Name (Mr./Mrs.)', 'type': 'text', 'required': True},
            {'name': 'emp_id', 'label': 'Employee ID / Labour ID', 'type': 'text'},
            {'name': 'designation', 'label': 'Designation', 'type': 'text', 'required': True},
            {'name': 'department', 'label': 'Attached Department / Division', 'type': 'text', 'required': True},
            {'name': 'office_site', 'label': 'Office / Site Location', 'type': 'text', 'default': 'RVJV PTE LTD - Earthwork and Enabling Works'},
            {'name': 'effective_date', 'label': 'Effective As Of Date', 'type': 'date', 'required': True},
            # Clearance Checkboxes
            {'name': 'store_clearance', 'label': 'Store Clearance (Tools, Equipment, Uniforms returned)', 'type': 'checkbox', 'default': True},
            {'name': 'store_remarks', 'label': 'Store Remarks / Pending', 'type': 'text', 'placeholder': 'Nil or pending items'},
            {'name': 'ohs_clearance', 'label': 'OHS Clearance (PPE, Safety Cards returned)', 'type': 'checkbox', 'default': True},
            {'name': 'ohs_remarks', 'label': 'OHS Remarks / Pending', 'type': 'text', 'placeholder': 'Nil or pending items'},
            {'name': 'accounts_clearance', 'label': 'Accounts Clearance (Imprest, Advances, Deductions cleared)', 'type': 'checkbox', 'default': True},
            {'name': 'accounts_remarks', 'label': 'Accounts Remarks / Pending', 'type': 'text', 'placeholder': 'Nil or pending items'},
            {'name': 'admin_clearance', 'label': 'Administration Clearance (Camp Room, Keys, Sim/Phone returned)', 'type': 'checkbox', 'default': True},
            {'name': 'admin_remarks', 'label': 'Administration Remarks / Pending', 'type': 'text', 'placeholder': 'Nil or pending items'},
            {'name': 'final_remarks', 'label': 'General / Final Remarks', 'type': 'textarea'},
        ]
    },
    'material_req': {
        'key': 'material_req',
        'title': 'Material Requisition Form',
        'subtitle': 'Project Material Purchase & Issue Request',
        'category': 'Procurement & Requisition',
        'category_icon': '📦',
        'icon': '🏗️',
        'badge_color': '#f59e0b',
        'badge_text': 'Materials',
        'template_file': 'Material Requisition.docx',
        'description': 'Formal requisition for project site materials, tools, spare parts, and consumables with itemized quantities, specifications and approval workflow.',
        'has_items_table': True,
        'fields': [
            {'name': 'project_name', 'label': 'Name of Project', 'type': 'text', 'default': 'Earthwork and Enabling Works'},
            {'name': 'order_ref_no', 'label': 'Requisition / Order Ref No', 'type': 'text', 'placeholder': 'Auto-assigned upon save or custom'},
            {'name': 'date_requisition', 'label': 'Date of Requisition', 'type': 'date', 'required': True},
            {'name': 'date_requirement', 'label': 'Date of Requirement / Urgency', 'type': 'date', 'required': True},
            {'name': 'to_recipient', 'label': 'To (Supplier / Store / Procurement Desk)', 'type': 'text', 'default': 'Procurement Department, RVJV PTE LTD'},
            {'name': 'department', 'label': 'Requesting Department / Section', 'type': 'text', 'required': True},
            {'name': 'requested_by', 'label': 'Requested By (Name & Designation)', 'type': 'text', 'required': True},
            {'name': 'approved_by', 'label': 'Approved By (Project Manager)', 'type': 'text', 'default': 'Project Manager'},
            {'name': 'accepted_by', 'label': 'Accepted By (Procurement Manager)', 'type': 'text', 'default': 'Procurement Manager'},
            {'name': 'remarks', 'label': 'General Notes / Purpose', 'type': 'textarea'},
        ]
    },
    'stationery_req': {
        'key': 'stationery_req',
        'title': 'Stationery Requisition Form',
        'subtitle': 'Office Stationery & Printing Supplies Order',
        'category': 'Procurement & Requisition',
        'category_icon': '📦',
        'icon': '📎',
        'badge_color': '#06b6d4',
        'badge_text': 'Stationery',
        'template_file': 'Stationery Requisition.docx',
        'description': 'Itemized requisition form for pens, paper, registers, files, printer cartridges, and general administrative stationery.',
        'has_items_table': True,
        'fields': [
            {'name': 'project_name', 'label': 'Name of Project', 'type': 'text', 'default': 'Earthwork and Enabling Works'},
            {'name': 'order_ref_no', 'label': 'Requisition / Order Ref No', 'type': 'text', 'placeholder': 'Auto-assigned upon save'},
            {'name': 'date_requisition', 'label': 'Date of Requisition', 'type': 'date', 'required': True},
            {'name': 'date_requirement', 'label': 'Date of Requirement', 'type': 'date', 'required': True},
            {'name': 'to_recipient', 'label': 'To (Admin Store / Office Store)', 'type': 'text', 'default': 'Administration Officer, RVJV PTE LTD'},
            {'name': 'department', 'label': 'Department / Unit', 'type': 'text', 'required': True},
            {'name': 'requested_by', 'label': 'Requested By (Name & Designation)', 'type': 'text', 'required': True},
            {'name': 'approved_by', 'label': 'Approved By (Project Manager)', 'type': 'text', 'default': 'Project Manager'},
            {'name': 'accepted_by', 'label': 'Accepted By (Admin Officer)', 'type': 'text', 'default': 'Adm Officer'},
            {'name': 'remarks', 'label': 'Purpose / Notes', 'type': 'textarea'},
        ]
    },
    'pantry_req': {
        'key': 'pantry_req',
        'title': 'Pantry Requisition Form',
        'subtitle': 'Pantry Supplies, Tea/Coffee & Refreshments',
        'category': 'Procurement & Requisition',
        'category_icon': '📦',
        'icon': '☕',
        'badge_color': '#ec4899',
        'badge_text': 'Pantry & Mess',
        'template_file': 'Pantry Requisition - Copy - Copy.docx',
        'description': 'Requisition for tea, milk, coffee, snacks, drinking water, disposable cups, cleaning supplies and kitchen consumables.',
        'has_items_table': True,
        'fields': [
            {'name': 'project_name', 'label': 'Name of Project', 'type': 'text', 'default': 'Earthwork and Enabling Works'},
            {'name': 'order_ref_no', 'label': 'Requisition / Order Ref No', 'type': 'text'},
            {'name': 'date_requisition', 'label': 'Date of Requisition', 'type': 'date', 'required': True},
            {'name': 'date_requirement', 'label': 'Date of Requirement', 'type': 'date', 'required': True},
            {'name': 'to_recipient', 'label': 'To (Pantry / Admin In-charge)', 'type': 'text', 'default': 'Admin In-charge, RVJV PTE LTD'},
            {'name': 'department', 'label': 'Department / Office Location', 'type': 'text', 'required': True},
            {'name': 'requested_by', 'label': 'Requested By (Name & Signature)', 'type': 'text', 'required': True},
            {'name': 'verified_by', 'label': 'Verified By (Adm Officer)', 'type': 'text', 'default': 'Adm Officer'},
            {'name': 'approved_by', 'label': 'Approved By (Project Manager)', 'type': 'text', 'default': 'Project Manager'},
            {'name': 'remarks', 'label': 'Remarks / Consumption Notes', 'type': 'textarea'},
        ]
    },
    'hr_req': {
        'key': 'hr_req',
        'title': 'Human Resource Requisition Form',
        'subtitle': 'New Position & Replacement Hiring Request',
        'category': 'HR & Clearance',
        'category_icon': '📋',
        'icon': '👔',
        'badge_color': '#6366f1',
        'badge_text': 'Manpower Requisition',
        'template_file': 'HUMAN RESOURCE REQUISITION FORM.pdf',
        'description': 'Formal requisition to HR/Management for hiring replacement or new manpower: position title, contract/regular/temporary, scope of work and qualifications.',
        'fields': [
            {'name': 'position_requested', 'label': 'Position Requested', 'type': 'text', 'required': True},
            {'name': 'department', 'label': 'Department / Division / Units', 'type': 'text', 'required': True},
            {'name': 'office_site', 'label': 'Office / Project Site', 'type': 'text', 'default': 'Earthwork and Enabling Works - RVJV'},
            {'name': 'requisition_type', 'label': 'Requisition Category', 'type': 'select', 'options': ['Existing Position / Replacement', 'New Position']},
            {'name': 'employment_type', 'label': 'Employment Type', 'type': 'select', 'options': ['On Contract', 'Regular', 'Temporary / Daily Wage']},
            {'name': 'num_vacancies', 'label': 'Number of Persons Required', 'type': 'number', 'default': 1, 'required': True},
            {'name': 'target_date', 'label': 'Target Joining Date', 'type': 'date'},
            {'name': 'job_description', 'label': 'Key Job Responsibilities / Scope of Work', 'type': 'textarea', 'required': True},
            {'name': 'min_qualifications', 'label': 'Minimum Qualifications / Experience Required', 'type': 'textarea'},
            {'name': 'justification', 'label': 'Reason / Justification for Request', 'type': 'textarea', 'required': True},
            {'name': 'requested_by_hod', 'label': 'Requested by Department Head', 'type': 'text', 'required': True},
            {'name': 'approved_by_pm', 'label': 'Approved by Project Manager', 'type': 'text', 'default': 'Project Manager'},
        ]
    },
    'temp_emp_info': {
        'key': 'temp_emp_info',
        'title': 'Temporary Employee Information Form',
        'subtitle': 'Letter Pad Format • Daily & Short-term Worker Dossier',
        'category': 'HR & Clearance',
        'category_icon': '📋',
        'icon': '📄',
        'badge_color': '#e11d48',
        'badge_text': 'Employee Dossier',
        'template_file': 'letter pad new.docx',
        'description': 'Employee intake and acknowledgment dossier for temporary, casual and sub-contractor personnel with emergency contact & address details.',
        'fields': [
            {'name': 'full_name', 'label': 'Full Name', 'type': 'text', 'required': True},
            {'name': 'address', 'label': 'Residential Address', 'type': 'text', 'required': True},
            {'name': 'dzongkhag', 'label': 'Dzongkhag / District', 'type': 'text'},
            {'name': 'gewog', 'label': 'Gewog / Sub-district', 'type': 'text'},
            {'name': 'village', 'label': 'Village / Town', 'type': 'text'},
            {'name': 'phone_number', 'label': 'Phone Number', 'type': 'text', 'required': True},
            {'name': 'email_address', 'label': 'Email Address', 'type': 'text'},
            {'name': 'emergency_contact_name', 'label': 'Emergency Contact Person', 'type': 'text', 'required': True},
            {'name': 'emergency_contact_phone', 'label': 'Emergency Contact Phone', 'type': 'text', 'required': True},
            {'name': 'designation', 'label': 'Designation / Role', 'type': 'text', 'required': True},
            {'name': 'department', 'label': 'Department / Unit', 'type': 'text', 'required': True},
            {'name': 'supervisor', 'label': 'Direct Supervisor', 'type': 'text'},
            {'name': 'start_date', 'label': 'Start Date', 'type': 'date', 'required': True},
            {'name': 'end_date', 'label': 'Expected End Date', 'type': 'date'},
            {'name': 'work_schedule', 'label': 'Work Schedule / Shift', 'type': 'text', 'default': 'General Shift (8:00 AM - 5:00 PM)'},
            {'name': 'remarks', 'label': 'Special Remarks', 'type': 'textarea'},
        ]
    }
}


@login_required
def office_forms_hub_view(request):
    """
    Main Office Forms & Formats Hub:
    Displays all 8 standardized forms grouped by category,
    summary stats, and the complete audit register of generated/printed forms.
    """
    search_q = request.GET.get('q', '').strip()
    selected_type = request.GET.get('type', '').strip()
    selected_status = request.GET.get('status', '').strip()

    # Query existing records
    records_qs = OfficeFormRecord.objects.select_related('created_by', 'updated_by', 'last_printed_by').all()

    if search_q:
        records_qs = records_qs.filter(
            Q(serial_no__icontains=search_q) |
            Q(form_title__icontains=search_q) |
            Q(created_by_name__icontains=search_q) |
            Q(notes__icontains=search_q)
        )
    if selected_type and selected_type in FORM_TEMPLATES_CONFIG:
        records_qs = records_qs.filter(form_type=selected_type)
    if selected_status:
        records_qs = records_qs.filter(status=selected_status)

    total_records = OfficeFormRecord.objects.count()
    total_printed = OfficeFormRecord.objects.filter(print_count__gt=0).count()
    recent_30_days = OfficeFormRecord.objects.filter(
        created_at__gte=timezone.now() - datetime.timedelta(days=30)
    ).count()

    # Group templates by category for the catalog view
    categories = {}
    for key, cfg in FORM_TEMPLATES_CONFIG.items():
        cat = cfg['category']
        if cat not in categories:
            categories[cat] = {
                'title': cat,
                'icon': cfg['category_icon'],
                'items': []
            }
        categories[cat]['items'].append(cfg)

    context = {
        'title': 'Office Forms & Print Hub',
        'categories': categories,
        'all_templates': FORM_TEMPLATES_CONFIG,
        'records': records_qs[:50],  # Recent 50
        'total_records': total_records,
        'total_printed': total_printed,
        'recent_30_days': recent_30_days,
        'search_q': search_q,
        'selected_type': selected_type,
        'selected_status': selected_status,
        'today': datetime.date.today().strftime('%d-%b-%Y'),
    }
    return render(request, 'office_forms/forms_hub.html', context)


@login_required
def office_form_editor_view(request, form_type, record_id=None):
    """
    Interactive form filler / editor for any of the 8 office forms.
    Pre-fills user details or existing saved record data.
    """
    if form_type not in FORM_TEMPLATES_CONFIG:
        raise Http404(f"Unknown form type: {form_type}")

    cfg = FORM_TEMPLATES_CONFIG[form_type]
    record = None
    form_data = {}

    if record_id:
        record = get_object_or_404(OfficeFormRecord, id=record_id)
        form_data = record.form_data or {}
    else:
        # Pre-fill smart defaults from user profile
        full_name = getattr(request.user, 'full_name', '') or request.user.get_full_name() or request.user.username
        emp_id = getattr(request.user, 'employee_id', '') or getattr(request.user, 'emp_id', '')
        designation = getattr(request.user, 'designation', '') or request.user.system_role
        department = getattr(request.user, 'department', '')

        form_data = {
            'worker_name': full_name,
            'applicant_name': full_name,
            'employee_name': full_name,
            'full_name': full_name,
            'requested_by': full_name,
            'requested_by_hod': full_name,
            'labour_id': emp_id,
            'emp_id': emp_id,
            'designation': designation,
            'department': department,
            'from_date': datetime.date.today().strftime('%Y-%m-%d'),
            'to_date': (datetime.date.today() + datetime.timedelta(days=1)).strftime('%Y-%m-%d'),
            'effective_date': datetime.date.today().strftime('%Y-%m-%d'),
            'date_requisition': datetime.date.today().strftime('%Y-%m-%d'),
            'date_requirement': (datetime.date.today() + datetime.timedelta(days=3)).strftime('%Y-%m-%d'),
            'start_date': datetime.date.today().strftime('%Y-%m-%d'),
            'target_date': (datetime.date.today() + datetime.timedelta(days=14)).strftime('%Y-%m-%d'),
            # Default empty items table for requisitions
            'items': [
                {'sl': 1, 'desc': '', 'qty': '', 'unit': 'Nos', 'spec': '', 'remark': ''},
                {'sl': 2, 'desc': '', 'qty': '', 'unit': 'Nos', 'spec': '', 'remark': ''},
                {'sl': 3, 'desc': '', 'qty': '', 'unit': 'Nos', 'spec': '', 'remark': ''},
            ]
        }

    context = {
        'cfg': cfg,
        'record': record,
        'form_data': form_data,
        'form_data_json': json.dumps(form_data),
        'today': datetime.date.today().strftime('%Y-%m-%d'),
    }
    return render(request, 'office_forms/form_editor.html', context)


@login_required
@require_POST
def api_save_office_form(request):
    """
    AJAX API endpoint to save / update form data and generate serial numbers.
    """
    try:
        data = json.loads(request.body)
        form_type = data.get('form_type')
        if form_type not in FORM_TEMPLATES_CONFIG:
            return JsonResponse({'success': False, 'message': 'Invalid form type'}, status=400)

        cfg = FORM_TEMPLATES_CONFIG[form_type]
        record_id = data.get('record_id')
        form_fields = data.get('form_data', {})
        status = data.get('status', 'SUBMITTED')
        notes = data.get('notes', '')

        user_name = getattr(request.user, 'full_name', '') or request.user.get_full_name() or request.user.username

        if record_id:
            record = get_object_or_404(OfficeFormRecord, id=record_id)
            record.form_data = form_fields
            record.updated_by = request.user
            record.status = status
            record.notes = notes
            record.save()
            action_msg = "Form updated successfully!"
        else:
            serial_no = OfficeFormRecord.generate_serial_no(form_type)
            record = OfficeFormRecord.objects.create(
                form_type=form_type,
                form_title=cfg['title'],
                serial_no=serial_no,
                created_by=request.user,
                created_by_name=user_name,
                updated_by=request.user,
                form_data=form_fields,
                status=status,
                notes=notes
            )
            action_msg = f"Form saved successfully! Serial No: {serial_no}"

        return JsonResponse({
            'success': True,
            'message': action_msg,
            'record_id': record.id,
            'serial_no': record.serial_no,
            'print_url': f"/forms/print/{record.id}/",
            'hub_url': "/forms/"
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
@require_POST
def api_log_office_form_print(request, record_id):
    """
    AJAX API endpoint called when user clicks Print / Generate PDF:
    Increments print count, logs user and timestamp for audit verification.
    """
    try:
        record = get_object_or_404(OfficeFormRecord, id=record_id)
        record.print_count += 1
        record.last_printed_at = timezone.now()
        record.last_printed_by = request.user
        if record.status == 'DRAFT':
            record.status = 'PRINTED'
        record.save(update_fields=['print_count', 'last_printed_at', 'last_printed_by', 'status'])

        return JsonResponse({
            'success': True,
            'print_count': record.print_count,
            'last_printed_at': record.last_printed_at.strftime('%d-%b-%Y %I:%M %p'),
            'printed_by': request.user.get_full_name() or request.user.username
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
def office_form_print_view(request, record_id):
    """
    Clean official A4 print layout for physical printing or saving as PDF.
    """
    record = get_object_or_404(OfficeFormRecord, id=record_id)
    cfg = FORM_TEMPLATES_CONFIG.get(record.form_type, {})
    form_data = record.form_data or {}

    # Pre-resolve signatures and dates safely in Python to avoid Django template lookup errors
    applicant_name = (
        form_data.get('requested_by') or
        form_data.get('worker_name') or
        form_data.get('applicant_name') or
        form_data.get('employee_name') or
        form_data.get('full_name') or
        record.created_by_name or
        'Applicant Signature'
    )

    recommended_by = (
        form_data.get('supervisor_name') or
        form_data.get('recommended_by') or
        form_data.get('verified_by') or
        form_data.get('requested_by_hod') or
        form_data.get('supervisor') or
        'HOD / Officer In-Charge'
    )

    approved_by = (
        form_data.get('approved_by') or
        form_data.get('approved_by_pm') or
        'Project Manager'
    )

    display_date = (
        form_data.get('date_requisition') or
        form_data.get('from_date') or
        form_data.get('start_date') or
        form_data.get('effective_date') or
        (record.created_at.strftime('%d-%b-%Y') if record.created_at else '')
    )

    context = {
        'record': record,
        'cfg': cfg,
        'data': form_data,
        'items': form_data.get('items', []),
        'print_time': timezone.now().strftime('%d-%b-%Y %I:%M %p'),
        'printed_by_user': request.user.get_full_name() or request.user.username,
        'applicant_signature_name': applicant_name,
        'recommended_by_name': recommended_by,
        'approved_by_name': approved_by,
        'display_date': display_date,
    }
    return render(request, 'office_forms/form_print_layout.html', context)



@login_required
def download_original_template(request, filename):
    """
    Serves the original unedited DOCX / PDF template file for download.
    """
    base_dir = os.path.join(settings.BASE_DIR, 'static', 'office_templates')
    safe_path = os.path.abspath(os.path.join(base_dir, filename))

    # Security check to prevent directory traversal
    if not safe_path.startswith(os.path.abspath(base_dir)) or not os.path.exists(safe_path):
        raise Http404("Template file not found.")

    return FileResponse(open(safe_path, 'rb'), as_attachment=True, filename=filename)
