import os
import json
import time
from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import transaction, connection
from portal.models import CampBlock, CampRoom, CampSetting, MessLocation, Employee, CampAssetAllotment

class Command(BaseCommand):
    help = "Seed and synchronize complete Camp Management data (Blocks, Rooms, Residents, Assets) from JSON fixture"

    def handle(self, *args, **options):
        fixture_path = os.path.join(settings.BASE_DIR, 'portal', 'fixtures', 'camp_complete_seed.json')
        if not os.path.exists(fixture_path):
            self.stderr.write(f"Fixture file not found at: {fixture_path}")
            return

        try:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA busy_timeout = 60000;")
        except Exception as e:
            pass

        self.stdout.write("Loading camp_complete_seed.json...")
        with open(fixture_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                with transaction.atomic():
                    self._run_seeding(data)
                break
            except Exception as e:
                if 'locked' in str(e).lower() and attempt < max_retries:
                    self.stdout.write(self.style.WARNING(f"Database is temporarily locked by web server, retrying in 3 seconds (Attempt {attempt}/{max_retries})..."))
                    time.sleep(3)
                else:
                    raise e

    def _run_seeding(self, data):
        # 1. Camp Settings
        settings_list = data.get('settings', [])
        if settings_list:
            s_data = settings_list[0]
            CampSetting.objects.update_or_create(
                id=1,
                defaults={
                    'camp_name': s_data.get('camp_name', 'Main Site Camp Residence'),
                    'latitude': s_data.get('latitude', 26.8935124),
                    'longitude': s_data.get('longitude', 90.4394126),
                    'geofence_radius_meters': s_data.get('geofence_radius_meters', 200),
                    'geofence_enabled': s_data.get('geofence_enabled', True),
                    'curfew_start_time': s_data.get('curfew_start_time', '22:00:00'),
                    'curfew_max_outside_hours': s_data.get('curfew_max_outside_hours', 4),
                    'voice_guidance_enabled': s_data.get('voice_guidance_enabled', False),
                }
            )
            self.stdout.write(self.style.SUCCESS("  [OK] Synced CampSetting"))

        # 2. Mess Locations
        mess_count = 0
        for m in data.get('mess_locations', []):
            code = m.get('code')
            if not code:
                continue
            MessLocation.objects.update_or_create(
                code=code,
                defaults={
                    'name': m.get('name', code),
                    'capacity': m.get('capacity', 200),
                    'contractor_agency': m.get('contractor_agency', ''),
                    'is_active': m.get('is_active', True),
                }
            )
            mess_count += 1
        self.stdout.write(self.style.SUCCESS(f"  [OK] Synced {mess_count} Mess Locations"))

        # 3. Camp Blocks
        block_map = {}
        block_count = 0
        for b in data.get('blocks', []):
            b_code = b.get('block_code')
            if not b_code:
                continue
            block_obj, _ = CampBlock.objects.update_or_create(
                block_code=b_code,
                defaults={
                    'name': b.get('name', f"Block {b_code}"),
                    'category': b.get('category', 'MIXED'),
                    'capacity': b.get('capacity', 160),
                    'caretaker_name': b.get('caretaker_name', ''),
                    'caretaker_contact': b.get('caretaker_contact', ''),
                }
            )
            block_map[b_code] = block_obj
            block_count += 1
        self.stdout.write(self.style.SUCCESS(f"  [OK] Synced {block_count} Camp Blocks (A to P)"))

        # 4. Camp Rooms
        room_map = {}
        room_count = 0
        for r in data.get('rooms', []):
            b_code = r.get('block_code')
            r_num = r.get('room_number')
            block_obj = block_map.get(b_code) or CampBlock.objects.filter(block_code=b_code).first()
            if not block_obj or not r_num:
                continue
            room_obj, _ = CampRoom.objects.update_or_create(
                block=block_obj,
                room_number=r_num,
                defaults={
                    'capacity': r.get('capacity', 6),
                    'room_key_number': r.get('room_key_number', r_num),
                    'key_status': r.get('key_status', 'IN_GATE_BOX'),
                    'fan_count': r.get('fan_count', 2),
                    'fan_status': r.get('fan_status', 'WORKING'),
                    'tubelight_count': r.get('tubelight_count', 2),
                    'tubelight_status': r.get('tubelight_status', 'WORKING'),
                    'door_lock_status': r.get('door_lock_status', 'WORKING'),
                }
            )
            room_map[r_num] = room_obj
            room_count += 1
        self.stdout.write(self.style.SUCCESS(f"  [OK] Synced {room_count} Camp Rooms"))

        # 5. Residents Allotments
        resident_synced = 0
        resident_created = 0
        for res in data.get('residents', []):
            emp_id = res.get('emp_id')
            if not emp_id:
                continue
            r_num = res.get('camp_room')
            emp = None
            if emp_id:
                emp = Employee.objects.filter(emp_id=emp_id).first()
            if not emp and res.get('name'):
                emp = Employee.objects.filter(name__iexact=res['name'].strip()).first()
            if emp:
                emp.camp_room = r_num
                if res.get('name') and not emp.name:
                    emp.name = res['name']
                if res.get('designation'):
                    emp.designation = res['designation']
                if res.get('contractor_agency'):
                    emp.contractor_agency = res['contractor_agency']
                if res.get('contact_info') and not emp.contact_info:
                    emp.contact_info = res['contact_info']
                if res.get('cid_number') and not emp.cid_number:
                    emp.cid_number = res['cid_number']
                if res.get('nationality'):
                    emp.nationality = res['nationality']
                if res.get('shift_remarks'):
                    emp.shift_remarks = res['shift_remarks']
                emp.save()
                resident_synced += 1
            else:
                Employee.objects.create(
                    emp_id=emp_id,
                    name=res.get('name', ''),
                    designation=res.get('designation', 'Worker'),
                    department=res.get('department', ''),
                    contact_info=res.get('contact_info', ''),
                    cid_number=res.get('cid_number', ''),
                    nationality=res.get('nationality', 'Expatriate'),
                    contractor_agency=res.get('contractor_agency', 'Company'),
                    camp_room=r_num,
                    status=res.get('status', 'Active'),
                    shift_remarks=res.get('shift_remarks', ''),
                )
                resident_created += 1
        self.stdout.write(self.style.SUCCESS(f"  [OK] Synced {resident_synced} existing employees and created {resident_created} missing camp resident workers"))

        # 6. Camp Assets Allotment
        asset_count = 0
        for a in data.get('assets', []):
            emp_id = a.get('emp_id')
            a_name = a.get('name', '')
            r_num = a.get('room_number')
            emp = None
            if emp_id:
                emp = Employee.objects.filter(emp_id=emp_id).first()
            if not emp and a_name:
                emp = Employee.objects.filter(name__iexact=a_name.strip()).first()
            rm = room_map.get(r_num) or CampRoom.objects.filter(room_number=r_num).first()
            if not emp or not rm:
                continue
            CampAssetAllotment.objects.update_or_create(
                employee=emp,
                defaults={
                    'room': rm,
                    'bed_number': a.get('bed_number', 'Bed 1'),
                    'bed_cot_allotted': a.get('bed_cot_allotted', True),
                    'mattress_allotted': a.get('mattress_allotted', True),
                    'pillow_allotted': a.get('pillow_allotted', True),
                    'fan_allotted': a.get('fan_allotted', True),
                    'blanket_allotted': a.get('blanket_allotted', True),
                    'bucket_mug_issued': a.get('bucket_mug_issued', True),
                    'locker_key_issued': a.get('locker_key_issued', False),
                    'locker_number': a.get('locker_number', ''),
                    'condition': a.get('condition', 'GOOD'),
                    'issue_date': a.get('issue_date', '2026-09-11'),
                }
            )
            asset_count += 1
        self.stdout.write(self.style.SUCCESS(f"  [OK] Synced {asset_count} Camp Asset Allotments"))
        self.stdout.write(self.style.SUCCESS("[OK] Camp Complete Data Seeding Finished Successfully!"))
