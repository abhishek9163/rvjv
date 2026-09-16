from django.utils.safestring import mark_safe
from django.utils.html import format_html
from django.utils.timesince import timesince
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.http import HttpResponse
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

@admin.action(description='Export Selected to Excel (.xlsx)')
def export_as_excel(modeladmin, request, queryset):
    meta = modeladmin.model._meta
    field_names = [field.name for field in meta.fields]
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename={meta.model_name}s.xlsx'
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = meta.model_name.capitalize()[:30]
    
    # Headers styling matching corporate theme
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(left=Side(style='thin', color='DDDDDD'),
                         right=Side(style='thin', color='DDDDDD'),
                         top=Side(style='thin', color='DDDDDD'),
                         bottom=Side(style='thin', color='DDDDDD'))
    
    # Write headers
    headers = [field.verbose_name.upper() if hasattr(field, 'verbose_name') else field.name.upper() for field in meta.fields]
    ws.append(headers)
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        
    # Write data rows
    for obj in queryset:
        row = []
        for field in field_names:
            val = getattr(obj, field)
            if val is None:
                val = ""
            elif hasattr(val, 'strftime'):
                val = val.strftime('%Y-%m-%d %H:%M:%S') if hasattr(val, 'hour') else val.strftime('%Y-%m-%d')
            elif hasattr(val, 'url'):
                val = request.build_absolute_uri(val.url)
            else:
                val = str(val)
            row.append(val)
        ws.append(row)
        
    # Autofit column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 10)
        for cell in col:
            cell.border = thin_border
            
    wb.save(response)
    return response

# Custom Admin supporting advanced top-bar export buttons
class ExportableAdmin(admin.ModelAdmin):
    change_list_template = 'admin/custom_change_list.html'
    excel_export_url = True
    pdf_export_url = True
    
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['excel_export_url'] = self.excel_export_url
        extra_context['pdf_export_url'] = self.pdf_export_url
        # Pass model meta info safely (templates can't access _meta directly)
        extra_context['model_fields'] = [
            {'name': f.name, 'verbose_name': str(f.verbose_name).title()}
            for f in self.model._meta.fields
        ]
        extra_context['model_name'] = self.model._meta.model_name
        return super().changelist_view(request, extra_context=extra_context)
        
    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path('export-excel-admin/', self.admin_site.admin_view(self.export_excel_admin_view), name=f'{self.model._meta.model_name}_export_excel_admin'),
            path('export-pdf-admin/', self.admin_site.admin_view(self.export_pdf_admin_view), name=f'{self.model._meta.model_name}_export_pdf_admin'),
        ]
        return custom_urls + urls

    def export_excel_admin_view(self, request):
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        
        # Get the filtered queryset
        cl = self.get_changelist_instance(request)
        queryset = cl.get_queryset(request)
        
        meta = self.model._meta
        
        # Read column parameter
        columns_param = request.GET.get('columns')
        if columns_param:
            field_names = [f.strip() for f in columns_param.split(',') if f.strip()]
        else:
            field_names = [field.name for field in meta.fields]
            
        # Optional vendor filtering
        vendor_name = request.GET.get('vendor_name')
        if vendor_name:
            if hasattr(self.model, 'supplier'):
                queryset = queryset.filter(supplier=vendor_name)
            elif hasattr(self.model, 'owner_name'):
                queryset = queryset.filter(owner_name=vendor_name)
                
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename={meta.model_name}s_filtered.xlsx'
        
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = meta.model_name.capitalize()[:30]
        
        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        thin_border = Border(left=Side(style='thin', color='DDDDDD'),
                             right=Side(style='thin', color='DDDDDD'),
                             top=Side(style='thin', color='DDDDDD'),
                             bottom=Side(style='thin', color='DDDDDD'))
        
        # Build headers
        headers = []
        for fn in field_names:
            try:
                field = meta.get_field(fn)
                headers.append(field.verbose_name.upper() if hasattr(field, 'verbose_name') else fn.upper())
            except:
                headers.append(fn.upper())
                
        ws.append(headers)
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            
        for obj in queryset:
            row = []
            for field in field_names:
                try:
                    val = getattr(obj, field)
                    if val is None:
                        val = ""
                    elif hasattr(val, 'strftime'):
                        val = val.strftime('%Y-%m-%d %H:%M:%S') if hasattr(val, 'hour') else val.strftime('%Y-%m-%d')
                    elif hasattr(val, 'url'):
                        val = request.build_absolute_uri(val.url)
                    elif hasattr(val, 'username'):  # For ForeignKeys e.g. User or related object
                        val = str(val)
                    else:
                        val = str(val)
                except:
                    val = ""
                row.append(val)
            ws.append(row)
            
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 10)
            for cell in col:
                cell.border = thin_border
                
        wb.save(response)
        return response

    def export_pdf_admin_view(self, request):
        from django.utils import timezone
        
        cl = self.get_changelist_instance(request)
        queryset = cl.get_queryset(request)
        
        meta = self.model._meta
        
        columns_param = request.GET.get('columns')
        if columns_param:
            field_names = [f.strip() for f in columns_param.split(',') if f.strip()]
        else:
            field_names = [field.name for field in meta.fields]
            
        # Optional vendor filtering
        vendor_name = request.GET.get('vendor_name')
        if vendor_name:
            if hasattr(self.model, 'supplier'):
                queryset = queryset.filter(supplier=vendor_name)
            elif hasattr(self.model, 'owner_name'):
                queryset = queryset.filter(owner_name=vendor_name)
                
        headers = []
        for fn in field_names:
            try:
                field = meta.get_field(fn)
                headers.append(field.verbose_name.upper() if hasattr(field, 'verbose_name') else fn.upper())
            except:
                headers.append(fn.upper())
        
        html = f"""
        <html>
        <head>
            <title>{meta.verbose_name_plural.title()}</title>
            <style>
                body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 20px; color: #333; }}
                h2 {{ text-align: center; color: #1e3a8a; margin-bottom: 20px; text-transform: uppercase; font-size: 16px; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 10px; }}
                th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
                th {{ background-color: #1e3a8a; color: white; font-weight: bold; text-transform: uppercase; }}
                tr:nth-child(even) {{ background-color: #f8fafc; }}
                .footer {{ text-align: right; margin-top: 20px; font-size: 9px; color: #666; }}
                @media print {{
                    @page {{ size: landscape; margin: 1cm; }}
                    button {{ display: none; }}
                }}
            </style>
        </head>
        <body>
            <h2>{meta.verbose_name_plural.title()} Report</h2>
            <table>
                <thead>
                    <tr>
        """
        
        for h in headers:
            html += f"<th>{h}</th>"
        html += "</tr></thead><tbody>"
        
        for obj in queryset:
            html += "<tr>"
            for field in field_names:
                try:
                    val = getattr(obj, field)
                    if val is None:
                        val = ""
                    elif hasattr(val, 'strftime'):
                        val = val.strftime('%Y-%m-%d %H:%M:%S') if hasattr(val, 'hour') else val.strftime('%Y-%m-%d')
                    elif hasattr(val, 'url'):
                        val = request.build_absolute_uri(val.url)
                    else:
                        val = str(val)
                except:
                    val = ""
                html += f"<td>{val}</td>"
            html += "</tr>"
            
        html += f"""
                </tbody>
            </table>
            <div class="footer">Generated on: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            <script>window.onload = function() {{ window.print(); }}</script>
        </body>
        </html>
        """
        return HttpResponse(html)

from .models import MessLog, MessGuestCoupon, MessMenu, User, Vehicle, CompanyPost, SystemSettings, Employee, EmployeeDocument, VehicleDocument, InsuranceDocument, Notification, DailyDeployment, UserActivityLog, OvertimeRecord, EmployeeAttendance

def export_as_excel(modeladmin, request, queryset):
    import pandas as pd
    from io import BytesIO
    from django.http import HttpResponse
    
    opts = modeladmin.model._meta
    data = list(queryset.values())
    df = pd.DataFrame(data)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=opts.verbose_name_plural[:31])
    output.seek(0)
    
    response = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename={opts.verbose_name_plural}.xlsx'
    return response

export_as_excel.short_description = "📊 Export Selected Records to Excel"


from django import forms

ALL_MODULE_CHOICES = (
    ('deployments', '📋 Daily Deployment'),
    ('shifts', '🕒 Shift Roster'),
    ('employees', '👥 Employees Directory'),
    ('fleet', '🚜 Fleet Vehicles'),
    ('movements', '🛣️ Vehicle Movements'),
    ('lubricants', '🛢️ Lubricants Consumed'),
    ('tyre', '🛞 Tyre Fitments'),
    ('spare_parts', '⚙️ Spare Parts Inventory'),
    ('attendance', '📊 Attendance & Muster Roll'),
    ('overtime', '⏳ Overtime & QR Punches'),
    ('documents', '📄 Documents & Expiries'),
    ('chat', '💬 Chat & Messaging'),
    ('system_settings', '⚙️ System Settings & Trash'),
    ('mess', '🍱 Mess Management'),
    ('breakdown_register', '🔧 Vehicle Breakdown Register & Entry'),
    ('vehicle_profiles', '🚗 Vehicle Profiles & 360° Directory'),
    ('safety_department', '🦺 Safety Department (PPE Store, Issues & Fines)'),
)

class CustomUserChangeForm(forms.ModelForm):
    assigned_modules = forms.MultipleChoiceField(
        choices=ALL_MODULE_CHOICES,
        widget=forms.CheckboxSelectMultiple(attrs={'style': 'margin-right: 6px; cursor: pointer;'}),
        required=False,
        help_text="Check the specific modules this user is allowed to access. (Superusers & Managers automatically have full access)."
    )

    class Meta:
        model = User
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            if isinstance(self.instance.assigned_modules, list):
                self.initial['assigned_modules'] = self.instance.assigned_modules

    def clean_assigned_modules(self):
        return list(self.cleaned_data.get('assigned_modules', []))


class CustomUserAdmin(UserAdmin):
    model = User
    form = CustomUserChangeForm

    fieldsets = (
        ('👤 Account Info & Profile', {
            'fields': ('username', 'password', 'full_name', 'email', 'phone_number', 'profile_picture')
        }),
        ('👑 System Role & Executive Control', {
            'fields': ('system_role', 'is_active', 'post', 'captain_category', 'enable_social_feed_mode')
        }),
        ('🧩 13-Module Permissions & Audit Rights', {
            'fields': ('assigned_modules', 'can_view_user_activity')
        }),
        ('🛡️ Advanced Django System Permissions', {
            'classes': ('collapse',),
            'fields': ('is_staff', 'is_superuser', 'groups', 'user_permissions')
        }),
        ('📜 Account Dates & User Activity Audit Trail', {
            'fields': ('last_login', 'date_joined', 'last_seen', 'get_user_recent_activity_table')
        }),
    )

    readonly_fields = ['get_user_recent_activity_table', 'last_login', 'date_joined', 'last_seen']

    list_display = [
        'username', 'full_name', 'email', 'get_system_role_badge',
        'enable_social_feed_mode', 'get_captain_category_badge', 'get_online_status',
        'get_module_permissions_summary', 'can_view_user_activity', 'is_active', 'is_staff'
    ]
    list_filter = ['system_role', 'enable_social_feed_mode', 'captain_category', 'can_view_user_activity', 'is_active', 'is_staff', 'is_superuser']
    search_fields = ['username', 'full_name', 'email', 'phone_number']
    ordering = ['-date_joined']


    @admin.action(description="⚡ Approve Selected Users (Set Role to DEO & Activate)")
    def approve_selected_users(self, request, queryset):
        cnt = queryset.update(system_role='DEO', is_active=True)
        self.message_user(request, f"Successfully approved {cnt} user(s) as DEO.")

    @admin.action(description="👑 Promote Selected Users to MANAGER")
    def make_users_manager(self, request, queryset):
        cnt = queryset.update(system_role='MANAGER')
        self.message_user(request, f"Promoted {cnt} user(s) to MANAGER role.")

    @admin.action(description="🕒 Set Selected Users to TIME_KEEPER")
    def make_users_timekeeper(self, request, queryset):
        cnt = queryset.update(system_role='TIME_KEEPER')
        self.message_user(request, f"Set {cnt} user(s) to TIME_KEEPER role.")

    @admin.action(description="🔒 Block / Deactivate Selected Accounts")
    def block_selected_users(self, request, queryset):
        cnt = queryset.update(is_active=False)
        self.message_user(request, f"Blocked {cnt} user account(s).")

    @admin.action(description="🔓 Activate / Unblock Selected Accounts")
    def activate_selected_users(self, request, queryset):
        cnt = queryset.update(is_active=True)
        self.message_user(request, f"Activated {cnt} user account(s).")

    @admin.action(description="🛡️ Grant User Activity Audit Logs Permission")
    def grant_activity_view_permission(self, request, queryset):
        cnt = queryset.update(can_view_user_activity=True)
        self.message_user(request, f"Granted audit view permission to {cnt} user(s).")

    @admin.action(description="🌐 Grant Full Module Permissions (All Modules)")
    def assign_full_module_permissions(self, request, queryset):
        all_mods = [
            'deployments', 'shifts', 'employees', 'fleet', 'movements',
            'lubricants', 'tyre', 'spare_parts', 'attendance', 'overtime',
            'documents', 'chat', 'system_settings', 'mess', 'breakdown_register', 'vehicle_profiles',
            'safety_department'
        ]
        for user in queryset:
            user.assigned_modules = all_mods
            user.save()
        self.message_user(request, f"Assigned full module access to {queryset.count()} user(s).")


    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [
        export_as_excel, approve_selected_users, make_users_manager,
        make_users_timekeeper, block_selected_users, activate_selected_users,
        grant_activity_view_permission, assign_full_module_permissions
    ]

    @admin.display(description='Role', ordering='system_role')
    def get_system_role_badge(self, obj):
        role = obj.system_role or 'PENDING'
        colors = {
            'MANAGER': 'background: #f3e8ff; color: #6b21a8; border: 1px solid #d8b4fe;',
            'PROJECT_MANAGER': 'background: #dbeafe; color: #1e40af; border: 1px solid #93c5fd;',
            'TIME_KEEPER': 'background: #fef3c7; color: #92400e; border: 1px solid #fde68a;',
            'DEO': 'background: #d1fae5; color: #065f46; border: 1px solid #6ee7b7;',
            'PENDING': 'background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5;'
        }
        style = colors.get(role, 'background: #f1f5f9; color: #475569;')
        return mark_safe(f'<span style="padding: 3px 8px; border-radius: 12px; font-weight: 800; font-size: 11px; {style}">{role}</span>')

    @admin.display(description='Online Status')
    def get_online_status(self, obj):
        if obj.is_online():
            return mark_safe('<span style="color: #10b981; font-weight: 800; font-size: 11px;">🟢 Online</span>')
        elif obj.last_seen:
            ts = timesince(obj.last_seen).split(",")[0]
            return mark_safe(f'<span style="color: #64748b; font-size: 11px;">⚪ {ts} ago</span>')
        return mark_safe('<span style="color: #94a3b8; font-size: 11px;">⚪ Offline</span>')

    @admin.display(description='Captain Category')
    def get_captain_category_badge(self, obj):
        cat = obj.captain_category or 'All'
        if cat == 'All':
            return mark_safe('<span style="color: #059669; font-weight: bold; font-size: 11px;">All Depts</span>')
        return mark_safe(f'<span style="background: #eff6ff; color: #1d4ed8; padding: 2px 6px; border-radius: 4px; font-weight: 700; font-size: 11px;">{cat}</span>')

    @admin.display(description='Assigned Modules')
    def get_module_permissions_summary(self, obj):
        if obj.system_role == 'MANAGER' or obj.is_superuser or not obj.assigned_modules:
            return mark_safe('<span style="color: #2563eb; font-weight: 700; font-size: 11px;">Full System Access</span>')
        return mark_safe(f'<span style="background: #f1f5f9; color: #334155; padding: 2px 6px; border-radius: 4px; font-weight: 700; font-size: 11px;">{len(obj.assigned_modules)} Modules</span>')


class UserActivityLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'user_name', 'user_role', 'action_type', 'module_name', 'description', 'ip_address']
    list_filter = ['action_type', 'module_name', 'user_role', 'created_at']
    search_fields = ['user_name', 'description', 'module_name', 'ip_address']
    readonly_fields = ['user', 'user_name', 'user_role', 'action_type', 'module_name', 'description', 'ip_address', 'created_at']
    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

class EmployeeAdmin(ExportableAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['emp_id', 'name', 'nationality', 'designation', 'department', 'status']
    list_filter = ['nationality', 'status', 'department']
    search_fields = ['emp_id', 'name', 'contact_info']

class EmployeeDocumentAdmin(admin.ModelAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['employee', 'document_type', 'document_number', 'issue_date', 'expiry_date']
    list_filter = ['document_type']
    search_fields = ['document_number']

class VehicleDocumentAdmin(admin.ModelAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['registration_no', 'vehicle_type', 'company', 'chassis_no', 'engine_no', 'driver_operator', 'rc_expiry_date']
    search_fields = ['registration_no', 'driver_operator', 'chassis_no', 'engine_no', 'vehicle_type', 'company']

class InsuranceDocumentAdmin(admin.ModelAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['registration_no', 'policy_no', 'insurance_provider', 'expiry_date']
    search_fields = ['registration_no', 'policy_no', 'insurance_provider']

class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ['website_name', 'trash_retention_days', 'doc_expiry_threshold', 'default_working_shift_hours', 'enable_low_stock_alerts', 'enable_email_notifications']
    
    def has_add_permission(self, request):
        if self.model.objects.count() >= 1:
            return False
        return super().has_add_permission(request)
        
    def has_delete_permission(self, request, obj=None):
        return False

class DailyDeploymentAdmin(ExportableAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['date', 'machinery', 'zone_1_2_day', 'zone_1_2_night', 'borrow_area_day', 'borrow_area_night', 'culvert_area_day', 'culvert_area_night', 'batching_plant_day', 'batching_plant_night', 'crushing_plant_day', 'crushing_plant_night', 'road_maint_day', 'road_maint_night']
    list_filter = ['date', 'machinery']
    search_fields = ['machinery']

class NotificationAdmin(admin.ModelAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['user', 'title', 'notification_type', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read']

class OvertimeRecordAdmin(ExportableAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['date', 'emp_id_snapshot', 'employee_name', 'designation', 'department', 'shift', 'location_zone', 'punch_type', 'in_time', 'out_time', 'overtime_hours', 'overtime_amount', 'status', 'time_keeper_name']
    list_filter = ['date', 'shift', 'location_zone', 'status', 'department', 'punch_type']
    search_fields = ['emp_id_snapshot', 'employee_name', 'cid_number', 'account_number', 'time_keeper_name', 'vehicle_regn']
    ordering = ['-date', '-created_at']

class EmployeeAttendanceAdmin(ExportableAdmin):

    @admin.display(description='Recent Activity History (Last 15 Actions)')
    def get_user_recent_activity_table(self, obj):
        if not obj or not obj.pk:
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">Save user account first to view activity logs.</div>')
        logs = UserActivityLog.objects.filter(user=obj).order_by('-created_at')[:15]
        if not logs.exists():
            return mark_safe('<div style="color: #94a3b8; font-size: 0.85rem; padding: 10px;">No activity logged for this user yet.</div>')
        
        rows = []
        for log in logs:
            action_color = '#2563eb'
            if log.action_type == 'CREATE': action_color = '#16a34a'
            elif log.action_type == 'UPDATE': action_color = '#d97706'
            elif log.action_type == 'DELETE': action_color = '#dc2626'
            elif log.action_type == 'LOGIN': action_color = '#7c3aed'
            elif log.action_type == 'LOGOUT': action_color = '#4b5563'

            rows.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 0.8rem;">
                <td style="padding: 8px 12px; font-weight: 600; color: #64748b; white-space: nowrap;">{log.created_at.strftime('%d %b %Y, %H:%M')}</td>
                <td style="padding: 8px 12px;"><span style="background: {action_color}15; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-weight: 800; font-size: 10px;">{log.action_type}</span></td>
                <td style="padding: 8px 12px; font-weight: 700; color: #1e293b;">{log.module_name or '-'}</td>
                <td style="padding: 8px 12px; color: #334155;">{log.description}</td>
                <td style="padding: 8px 12px; color: #64748b; font-family: monospace;">{log.ip_address or '-'}</td>
            </tr>
            """)

        html = f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: #ffffff; margin-top: 5px;">
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 0.75rem; text-transform: uppercase; color: #475569;">
                        <th style="padding: 10px 12px;">Time</th>
                        <th style="padding: 10px 12px;">Action</th>
                        <th style="padding: 10px 12px;">Module</th>
                        <th style="padding: 10px 12px;">Description</th>
                        <th style="padding: 10px 12px;">IP Address</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(html)

    actions = [export_as_excel]
    list_display = ['date', 'get_employee_id', 'get_employee_name', 'get_department', 'shift', 'status', 'in_time', 'out_time', 'punch_source', 'marked_by']
    list_filter = ['date', 'shift', 'status', 'punch_source', 'employee__department']
    search_fields = ['employee__emp_id', 'employee__name', 'remarks']
    ordering = ['-date', 'employee__name']

    @admin.display(description='Emp ID', ordering='employee__emp_id')
    def get_employee_id(self, obj):
        return obj.employee.emp_id if obj.employee else '-'

    @admin.display(description='Employee Name', ordering='employee__name')
    def get_employee_name(self, obj):
        return obj.employee.name if obj.employee else '-'

    @admin.display(description='Department', ordering='employee__department')
    def get_department(self, obj):
        return obj.employee.department if obj.employee else '-'

admin.site.register(User, CustomUserAdmin)
admin.site.register(CompanyPost)
admin.site.register(SystemSettings, SystemSettingsAdmin)
admin.site.register(Employee, EmployeeAdmin)
admin.site.register(EmployeeDocument, EmployeeDocumentAdmin)
admin.site.register(VehicleDocument, VehicleDocumentAdmin)
admin.site.register(InsuranceDocument, InsuranceDocumentAdmin)
admin.site.register(DailyDeployment, DailyDeploymentAdmin)
admin.site.register(Notification, NotificationAdmin)
admin.site.register(Vehicle)
admin.site.register(UserActivityLog, UserActivityLogAdmin)
admin.site.register(OvertimeRecord, OvertimeRecordAdmin)
admin.site.register(EmployeeAttendance, EmployeeAttendanceAdmin)

# Patch admin.site.index to add vehicles and parts to admin index page context
original_index = admin.site.index

def custom_admin_index(request, extra_context=None):
    from fleet.models import FleetVehicle, SparePart
    extra_context = extra_context or {}
    try:
        extra_context['vehicles'] = list(FleetVehicle.objects.filter(is_active=True).order_by('regn'))
        extra_context['parts'] = list(SparePart.objects.all())
    except Exception:
        pass
    return original_index(request, extra_context=extra_context)

admin.site.index = custom_admin_index


class MessLogAdmin(ExportableAdmin):
    actions = [export_as_excel]
    list_display = ['punch_time', 'date', 'employee', 'meal_type', 'status', 'scanned_by', 'remarks']
    list_filter = ['date', 'meal_type', 'status']
    search_fields = ['employee__name', 'employee__emp_id', 'remarks']
    ordering = ['-punch_time']

class MessGuestCouponAdmin(admin.ModelAdmin):
    list_display = ['coupon_code', 'guest_name', 'contractor_company', 'meal_type', 'is_redeemed', 'redeemed_at']
    list_filter = ['meal_type', 'is_redeemed']
    search_fields = ['guest_name', 'coupon_code', 'contractor_company']

class MessMenuAdmin(admin.ModelAdmin):
    list_display = ['date', 'breakfast_menu', 'lunch_menu', 'dinner_menu']
    ordering = ['-date']

admin.site.register(MessLog, MessLogAdmin)
admin.site.register(MessGuestCoupon, MessGuestCouponAdmin)
admin.site.register(MessMenu, MessMenuAdmin)

from portal.models import FeedEntryLike, FeedEntryComment

@admin.register(FeedEntryLike)
class FeedEntryLikeAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'log_entry', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'user__full_name', 'log_entry__employee__name']

@admin.register(FeedEntryComment)
class FeedEntryCommentAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'log_entry', 'comment_text', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'comment_text', 'log_entry__employee__name']

from portal.models import EmployeeLeaveRecord, MessAssetItem, MessAssetAllocation, MessAssetReturnLog

@admin.register(EmployeeLeaveRecord)
class EmployeeLeaveRecordAdmin(admin.ModelAdmin):
    list_display = ['form_number', 'employee', 'leave_type', 'start_date', 'end_date', 'total_days', 'status', 'approved_by', 'created_at']
    list_filter = ['leave_type', 'status', 'start_date', 'end_date']
    search_fields = ['form_number', 'employee__name', 'employee__emp_id', 'destination', 'contact_number']

@admin.register(MessAssetItem)
class MessAssetItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'asset_code', 'mess_location', 'total_quantity', 'available_quantity', 'unit', 'condition_status']
    list_filter = ['category', 'condition_status', 'mess_location']
    search_fields = ['name', 'asset_code']

@admin.register(MessAssetAllocation)
class MessAssetAllocationAdmin(admin.ModelAdmin):
    list_display = ['asset', 'allocated_to_type', 'staff_member', 'staff_role', 'mess_location', 'quantity', 'issue_date', 'status']
    list_filter = ['allocated_to_type', 'status', 'staff_role', 'issue_date']
    search_fields = ['asset__name', 'staff_member__name', 'staff_member__emp_id', 'mess_location__name']

@admin.register(MessAssetReturnLog)
class MessAssetReturnLogAdmin(admin.ModelAdmin):
    list_display = ['allocation', 'return_date', 'returned_quantity', 'condition', 'received_by', 'created_at']
    list_filter = ['condition', 'return_date']
    search_fields = ['allocation__asset__name', 'allocation__staff_member__name']


from portal.models import SafetyStoreItem, SafetyEquipmentIssue, SafetyReplacementAndFine

@admin.register(SafetyStoreItem)
class SafetyStoreItemAdmin(admin.ModelAdmin):
    list_display = ['item_code', 'name', 'category', 'unit', 'total_stock', 'available_stock', 'issued_stock_display', 'warranty_months', 'fine_amount', 'is_low_stock_display']
    list_filter = ['category']
    search_fields = ['name', 'item_code', 'specification']
    actions = [export_as_excel]

    @admin.display(description="Issued Stock")
    def issued_stock_display(self, obj):
        return obj.issued_stock

    @admin.display(description="Stock Status")
    def is_low_stock_display(self, obj):
        if obj.is_low_stock:
            return mark_safe('<span style="color: #ef4444; font-weight: bold;">⚠️ Low Stock</span>')
        return mark_safe('<span style="color: #10b981; font-weight: bold;">✅ In Stock</span>')


@admin.register(SafetyEquipmentIssue)
class SafetyEquipmentIssueAdmin(admin.ModelAdmin):
    list_display = ['employee', 'item', 'quantity', 'size_specification', 'issue_date', 'warranty_expiry_date', 'warranty_status_display', 'status', 'issued_by']
    list_filter = ['status', 'issue_date', 'item__category']
    search_fields = ['employee__name', 'employee__emp_id', 'item__name', 'item__item_code']
    actions = [export_as_excel]

    @admin.display(description="Warranty Status")
    def warranty_status_display(self, obj):
        if obj.is_under_warranty:
            return mark_safe(f'<span style="color: #10b981; font-weight: bold;">🟢 Active ({obj.days_remaining}d left)</span>')
        return mark_safe('<span style="color: #64748b; font-weight: bold;">⚪ Expired</span>')


@admin.register(SafetyReplacementAndFine)
class SafetyReplacementAndFineAdmin(admin.ModelAdmin):
    list_display = ['employee', 'item', 'replacement_date', 'reason', 'is_premature', 'fine_amount', 'fine_status', 'processed_by']
    list_filter = ['reason', 'is_premature', 'fine_status', 'replacement_date']
    search_fields = ['employee__name', 'employee__emp_id', 'item__name', 'item__item_code', 'waived_reason']
    actions = [export_as_excel]


