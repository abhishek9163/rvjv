from .models import SystemSettings

def sidebar_modules(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        if user.is_superuser:
            user_modules = [
                'daily_deployment', 'shift_management', 'vehicle_movement', 'tyre_section',
                'total_employees', 'foreign_workers', 'national_workers', 'total_vehicles',
                'lubricants', 'document_expiries', 'spare_parts', 'attendance_management', 'overtime_management', 'manage_logins',
                'breakdown_register', 'vehicle_profiles', 'safety_department'
            ]
        elif user.system_role == 'MANAGER':
            base_m = [
                'daily_deployment', 'shift_management', 'vehicle_movement', 'tyre_section',
                'total_employees', 'foreign_workers', 'national_workers', 'total_vehicles',
                'lubricants', 'document_expiries', 'spare_parts', 'attendance_management',
                'breakdown_register', 'vehicle_profiles', 'safety_department'
            ]
            assigned = list(user.assigned_modules or [])
            if 'overtime_management' in assigned:
                base_m.append('overtime_management')
            if 'manage_logins' in assigned:
                base_m.append('manage_logins')
            if 'attendance_management' in assigned and 'attendance_management' not in base_m:
                base_m.append('attendance_management')
            if 'safety_department' in assigned and 'safety_department' not in base_m:
                base_m.append('safety_department')
            user_modules = base_m
        elif user.system_role in ['TIME_KEEPER', 'PROJECT_MANAGER']:
            base_tk = ['attendance_management', 'overtime_management']
            for m in list(user.assigned_modules or []):
                if m not in base_tk:
                    base_tk.append(m)
            user_modules = base_tk
        else:
            user_modules = list(user.assigned_modules or [])
            if 'total_employees' in user_modules:
                if 'foreign_workers' not in user_modules:
                    user_modules.append('foreign_workers')
                if 'national_workers' not in user_modules:
                    user_modules.append('national_workers')
    else:
        user_modules = []
    return {
        'sidebar_sections': [],
        'system_settings': SystemSettings.get_settings(),
        'user_modules': user_modules,
    }

