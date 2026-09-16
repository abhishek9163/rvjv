import os
import json
from datetime import date, datetime
from django.core.management.base import BaseCommand
from portal.models import SafetyStoreItem, SafetyEquipmentIssue, SafetyReplacementAndFine

class Command(BaseCommand):
    help = 'Dump or Load Safety department data for server sync'

    def add_arguments(self, parser):
        parser.add_argument('--export', action='store_true', help='Export safety data to JSON')
        parser.add_argument('--import', dest='import_data', action='store_true', help='Import safety data from JSON')

    def handle(self, *args, **options):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        json_file = os.path.join(base_dir, 'safety_data_export.json')

        if options.get('import_data'):
            if not os.path.exists(json_file):
                self.stdout.write(self.style.ERROR(f'File not found: {json_file}'))
                return
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.stdout.write('Syncing Safety Store Items...')
            for item_data in data.get('items', []):
                SafetyStoreItem.objects.update_or_create(
                    item_code=item_data['item_code'],
                    defaults={
                        'name': item_data['name'],
                        'category': item_data.get('category', 'General'),
                        'unit': item_data.get('unit', 'Pcs'),
                        'specification': item_data.get('specification', ''),
                        'total_stock': item_data.get('total_stock', 0),
                        'available_stock': item_data.get('available_stock', 0),
                        'minimum_alert_level': item_data.get('minimum_alert_level', 10),
                        'warranty_months': item_data.get('warranty_months', 6),
                        'fine_amount': item_data.get('fine_amount', 0),
                        'notes': item_data.get('notes', ''),
                    }
                )

            self.stdout.write('Syncing Safety Equipment Issues...')
            from portal.models import Employee
            issues_created = 0
            issues_updated = 0
            for iss in data.get('issues', []):
                emp = None
                if iss.get('emp_id'):
                    emp = Employee.objects.filter(emp_id=iss['emp_id']).first()
                if not emp and iss.get('emp_name'):
                    emp = Employee.objects.filter(name__iexact=iss['emp_name']).first()
                if not emp:
                    continue

                item = SafetyStoreItem.objects.filter(item_code=iss['item_code']).first()
                if not item:
                    continue

                issue_d = datetime.strptime(iss['issue_date'], '%Y-%m-%d').date() if iss.get('issue_date') else None
                exp_d = datetime.strptime(iss['warranty_expiry_date'], '%Y-%m-%d').date() if iss.get('warranty_expiry_date') else None

                obj, created = SafetyEquipmentIssue.objects.update_or_create(
                    employee=emp,
                    item=item,
                    issue_date=issue_d,
                    defaults={
                        'quantity': iss.get('quantity', 1),
                        'size_specification': iss.get('size_specification', ''),
                        'warranty_expiry_date': exp_d,
                        'status': iss.get('status', 'ACTIVE'),
                        'remarks': iss.get('remarks', ''),
                    }
                )
                if created:
                    issues_created += 1
                else:
                    issues_updated += 1

            self.stdout.write(self.style.SUCCESS(f'Safety sync complete! Created: {issues_created}, Updated: {issues_updated}'))

        else:
            self.stdout.write('Exporting Safety Store Items and Issues...')
            items_list = []
            for item in SafetyStoreItem.objects.all():
                items_list.append({
                    'id': item.id,
                    'item_code': item.item_code,
                    'name': item.name,
                    'category': item.category,
                    'unit': item.unit,
                    'specification': item.specification,
                    'total_stock': item.total_stock,
                    'available_stock': item.available_stock,
                    'minimum_alert_level': item.minimum_alert_level,
                    'warranty_months': item.warranty_months,
                    'fine_amount': float(item.fine_amount or 0),
                    'notes': item.notes,
                })

            issues_list = []
            for iss in SafetyEquipmentIssue.objects.select_related('employee', 'item').all():
                issues_list.append({
                    'id': iss.id,
                    'emp_id': iss.employee.emp_id if iss.employee else '',
                    'emp_name': iss.employee.name if iss.employee else '',
                    'item_code': iss.item.item_code if iss.item else '',
                    'quantity': iss.quantity,
                    'size_specification': iss.size_specification,
                    'issue_date': iss.issue_date.strftime('%Y-%m-%d') if iss.issue_date else None,
                    'warranty_expiry_date': iss.warranty_expiry_date.strftime('%Y-%m-%d') if iss.warranty_expiry_date else None,
                    'status': iss.status,
                    'remarks': iss.remarks or '',
                })

            export_data = {
                'items': items_list,
                'issues': issues_list,
                'exported_at': datetime.now().isoformat()
            }

            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2)

            self.stdout.write(self.style.SUCCESS(f'Exported {len(items_list)} items and {len(issues_list)} issues to {json_file}'))
