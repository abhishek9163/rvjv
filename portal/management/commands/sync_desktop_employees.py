# portal/management/commands/sync_desktop_employees.py
import os
import json
from django.core.management.base import BaseCommand
from django.conf import settings
from django.core.management import call_command
from portal.models import Employee

class Command(BaseCommand):
    help = "Import and sync Employee dataset from fixture or Desktop Excel sheets"

    def handle(self, *args, **options):
        # 1. Try loading from all_employees_master.json fixture first
        fixture_path = os.path.join(settings.BASE_DIR, 'portal', 'fixtures', 'all_employees_master.json')
        if os.path.exists(fixture_path):
            self.stdout.write(f"Loading complete employee dataset from fixture: {fixture_path}...")
            try:
                call_command('loaddata', fixture_path)
                self.stdout.write(self.style.SUCCESS(f"Successfully loaded fixture! Total Employees in DB: {Employee.objects.count()}"))
                return
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Fixture loaddata notice: {e}, falling back to Excel parsing..."))

        # 2. Fallback to reading Excel files if present
        src_paths = [
            r'C:\Users\adev9\OneDrive\Desktop\Employee (3).xlsx',
            r'C:\Users\adev9\Desktop\Employee (3).xlsx',
        ]
        
        target_src = None
        for p in src_paths:
            if os.path.exists(p):
                target_src = p
                break
                
        if not target_src:
            self.stdout.write(f"No Excel source found. Current Employees in DB: {Employee.objects.count()}")
            return

        import pandas as pd
        self.stdout.write(f"Reading {target_src}...")
        df = pd.read_excel(target_src, sheet_name='Employee')
        created_count = 0
        updated_count = 0

        for idx, row in df.iterrows():
            emp_id = str(row['ID']).strip() if pd.notna(row['ID']) else None
            if not emp_id or emp_id.lower() == 'nan':
                continue

            full_name = str(row['Full Name']).strip() if pd.notna(row['Full Name']) else ''
            designation = str(row['Designation']).strip() if pd.notna(row['Designation']) else ''
            department = str(row['Department']).strip() if pd.notna(row['Department']) else ''
            contact_info = str(row['Mobile Number']).strip() if pd.notna(row['Mobile Number']) else ''
            cid_number = str(row['CID Number']).strip() if pd.notna(row['CID Number']) else ''
            nationality = str(row['Nationality']).strip() if pd.notna(row['Nationality']) else 'Bhutanese'
            raw_status = str(row['Status']).strip() if pd.notna(row['Status']) else 'Active'

            if raw_status.lower() in ['terminated', 'resigned', 'left', 'inactive', '0']:
                status = 'Terminated'
            else:
                status = 'Active'

            emp, created = Employee.objects.get_or_create(
                emp_id=emp_id,
                defaults={
                    'name': full_name,
                    'designation': designation,
                    'department': department,
                    'contact_info': contact_info,
                    'cid_number': cid_number,
                    'nationality': nationality,
                    'status': status,
                }
            )

            if created:
                created_count += 1
            else:
                emp.name = full_name or emp.name
                emp.designation = designation or emp.designation
                emp.department = department or emp.department
                if contact_info: emp.contact_info = contact_info
                if cid_number: emp.cid_number = cid_number
                if nationality: emp.nationality = nationality
                emp.save()
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully synced! Created: {created_count}, Updated: {updated_count}, Total Employees in DB: {Employee.objects.count()}"))

