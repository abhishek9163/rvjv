from datetime import date, time
from django.test import TestCase
from django.urls import reverse
from portal.models import Employee, User
from .models import FleetVehicle, HiredVehicle, TyreLog, VehicleMovement


class VehicleMovementTests(TestCase):
    def setUp(self):
        self.vehicle = FleetVehicle.objects.create(dno='D-01', regn='TEST-101', model_name='Truck')
        self.driver = Employee.objects.create(emp_id='E-1', name='Test Driver', nationality='Indian', designation='Driver', department='P&M')
        self.deo = User.objects.create_user(username='deo', password='pass123', system_role='DEO', assigned_modules=['vehicle_movement'])
        self.client.force_login(self.deo)

    def test_deo_can_create_movement_and_sync_shift(self):
        response = self.client.post(reverse('fleet:add_vehicle_movement'), {
            'vehicle_number': 'TEST-101', 'driver_name': 'Test Driver',
            'movement_date': '2026-08-26', 'movement_time': '09:15',
            'shift': 'Night', 'destination': 'Zone 1', 'purpose': 'Material delivery',
        })
        self.assertRedirects(response, reverse('fleet:vehicle_movements'))
        movement = VehicleMovement.objects.get()
        self.assertEqual(movement.driver_name, 'Test Driver')
        self.assertEqual(movement.vehicle_number, 'TEST-101')
        # Shift Management is updated automatically (no checkbox needed)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.current_shift, 'Night')
        self.assertEqual(self.driver.assigned_vehicle, self.vehicle)  # matched TEST-101 in fleet master

    def test_new_driver_is_added_to_shift_management(self):
        self.client.post(reverse('fleet:add_vehicle_movement'), {
            'vehicle_number': 'TEST-101', 'driver_name': 'Brand New Driver',
            'movement_date': '2026-08-26', 'movement_time': '23:00',
            'shift': 'Night',
        })
        emp = Employee.objects.filter(name__iexact='Brand New Driver').first()
        self.assertIsNotNone(emp)
        self.assertEqual(emp.designation, 'Driver')
        self.assertEqual(emp.current_shift, 'Night')
        self.assertEqual(emp.assigned_vehicle, self.vehicle)

    def test_edit_keeps_same_history_record(self):
        movement = VehicleMovement.objects.create(vehicle_number='TEST-101', driver_name='Test Driver', movement_date=date(2026, 8, 26), movement_time=time(9, 15), shift='Day', entered_by=self.deo)
        self.client.post(reverse('fleet:edit_vehicle_movement', args=[movement.id]), {
            'vehicle_number': 'NEW-999', 'driver_name': 'New Driver',
            'movement_date': '2026-08-27', 'movement_time': '10:30', 'shift': 'Day',
        })
        self.assertEqual(VehicleMovement.objects.count(), 1)
        movement.refresh_from_db()
        self.assertEqual(movement.movement_date, date(2026, 8, 27))
        self.assertEqual(movement.vehicle_number, 'NEW-999')

    def test_unassigned_deo_is_denied(self):
        self.deo.assigned_modules = []
        self.deo.save()
        response = self.client.get(reverse('fleet:vehicle_movements'))
        self.assertRedirects(response, reverse('dashboard'))

    def test_delete_movement(self):
        movement = VehicleMovement.objects.create(vehicle_number='TEST-101', driver_name='Test Driver', movement_date=date(2026, 8, 26), movement_time=time(9, 15), shift='Day', entered_by=self.deo)
        self.client.post(reverse('fleet:delete_vehicle_movement', args=[movement.id]))
        self.assertEqual(VehicleMovement.objects.count(), 0)

    def test_export_excel(self):
        VehicleMovement.objects.create(vehicle_number='TEST-101', driver_name='Test Driver', movement_date=date(2026, 8, 26), movement_time=time(9, 15), shift='Day', entered_by=self.deo)
        response = self.client.get(reverse('fleet:movements_export'), {'format': 'excel', 'columns': 'vehicle_number,driver_name,shift'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])

    def test_directory_filters_by_vehicle(self):
        VehicleMovement.objects.create(vehicle_number='AAA-1', driver_name='Driver A', movement_date=date(2026, 8, 26), movement_time=time(9, 15), shift='Day', entered_by=self.deo)
        VehicleMovement.objects.create(vehicle_number='BBB-2', driver_name='Driver B', movement_date=date(2026, 8, 27), movement_time=time(10, 0), shift='Night', entered_by=self.deo)
        response = self.client.get(reverse('fleet:movement_directory'), {'vehicle': 'AAA-1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['movements']), 1)
        self.assertEqual(response.context['movements'][0].driver_name, 'Driver A')

    def test_directory_export_excel_and_pdf(self):
        VehicleMovement.objects.create(vehicle_number='AAA-1', driver_name='Driver A', movement_date=date(2026, 8, 26), movement_time=time(9, 15), shift='Day', entered_by=self.deo)
        base = {'date_from': '2026-08-01', 'date_to': '2026-08-31'}
        excel = self.client.get(reverse('fleet:directory_export'), {**base, 'format': 'excel', 'driver': 'Driver A'})
        self.assertEqual(excel.status_code, 200)
        self.assertIn('spreadsheetml', excel['Content-Type'])
        pdf = self.client.get(reverse('fleet:directory_export'), {**base, 'format': 'pdf', 'vehicle': 'AAA-1'})
        self.assertEqual(pdf.status_code, 200)
        self.assertIn(b'Fleet Records', pdf.content)

    def test_deo_dashboard_shows_vehicle_movement_card(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/fleet/movements/')
        self.assertContains(response, 'Vehicle Movement Register')

    def test_export_extra_filters(self):
        VehicleMovement.objects.create(vehicle_number='AAA-1', driver_name='Driver A', movement_date=date(2026, 8, 26), movement_time=time(9, 15), shift='Day', destination='Workshop', entered_by=self.deo)
        VehicleMovement.objects.create(vehicle_number='BBB-2', driver_name='Driver B', movement_date=date(2026, 8, 27), movement_time=time(10, 0), shift='Night', destination='Zone 1', entered_by=self.deo)
        pdf = self.client.get(reverse('fleet:movements_export'), {'format': 'pdf', 'shift': 'Night'})
        self.assertEqual(pdf.status_code, 200)
        self.assertIn('1 record(s)', pdf.content.decode('utf-8', 'ignore'))
        live_dest = self.client.get(reverse('fleet:movements_live_api'), {'destination': 'Zone'})
        self.assertEqual(live_dest.json()['count'], 1)
        live_type = self.client.get(reverse('fleet:movements_live_api'), {'vehicle_type': 'Truck'})
        self.assertEqual(live_type.json()['count'], 0)  # no vehicle linked on these rows
        dexp = self.client.get(reverse('fleet:directory_export'), {'format': 'pdf', 'shift': 'Day', 'destination': 'workshop'})
        self.assertEqual(dexp.status_code, 200)
        self.assertIn('1 record(s)', dexp.content.decode('utf-8', 'ignore'))


class TyreSectionTests(TestCase):
    def setUp(self):
        self.vehicle = FleetVehicle.objects.create(dno='D-01', regn='TEST-101', model_name='Truck')
        self.deo = User.objects.create_user(username='tyre_deo', password='pass123', system_role='DEO', assigned_modules=['tyre_section'])
        self.client.force_login(self.deo)

    def test_deo_can_add_tyre_entry_with_auto_total(self):
        response = self.client.post(reverse('fleet:api_add_tyre'), {
            'vehicle_id': self.vehicle.id, 'date': '2026-08-26',
            'location': 'Gelephu', 'work_order_no': 'RVJ/HA/HI/26-03-8',
            'punctures': 1, 'big_patches': 1, 'small_patches': 0, 'nozzles': 0,
            'material_cost': '500', 'big_patches_cost': '500',
            'small_patches_cost': '0', 'opening_fitting_cost': '300',
        })
        self.assertRedirects(response, '/fleet/tyre/')
        log = TyreLog.objects.get()
        # TOTAL AMOUNT auto-calculated: 500 + 500 + 0 + 300
        self.assertEqual(float(log.total_amount), 1300.00)
        self.assertEqual(log.punctures, 1)

    def test_deo_without_module_is_denied(self):
        self.deo.assigned_modules = []
        self.deo.save()
        response = self.client.get(reverse('fleet:tyre'))
        self.assertRedirects(response, reverse('dashboard'))

    def test_tyre_page_and_exports(self):
        TyreLog.objects.create(
            date=date(2026, 8, 26), location='Gelephu', work_order_no='RVJ/1', vehicle=self.vehicle,
            punctures=1, big_patches=1, material_cost=500, big_patches_cost=500, total_amount=1000, entered_by=self.deo)
        page = self.client.get(reverse('fleet:tyre'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '/fleet/api/tyre/add/')
        self.assertContains(page, 'lub-stats-grid')
        excel = self.client.get(reverse('fleet:api_export_tyre'))
        self.assertEqual(excel.status_code, 200)
        self.assertIn('spreadsheetml', excel['Content-Type'])
        pdf = self.client.get(reverse('fleet:api_export_tyre_pdf'))
        self.assertEqual(pdf.status_code, 200)
        self.assertIn(b'TYRE PUNCTURE', pdf.content)
        self.assertIn(b'data:image/png;base64', pdf.content)  # logo embedded

    def test_deo_dashboard_shows_tyre_card(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/fleet/tyre/')
        self.assertContains(response, 'Tyre Section')

    def test_vendor_autofill_from_hire_master(self):
        HiredVehicle.objects.create(
            regn='TEST-101', equipment_type='10 wheeler Tipper',
            owner_name='Pradeep Transport Service', agreement_ref='RVJ/HA/HI/26-04-50')
        # No vendor posted — backend must auto-fill from hire master
        self.client.post(reverse('fleet:api_add_tyre'), {
            'vehicle_id': self.vehicle.id, 'date': '2026-08-26',
            'punctures': 1, 'material_cost': '500',
        })
        log = TyreLog.objects.get()
        self.assertEqual(log.vendor, 'Pradeep Transport Service')
        # WO No also auto-filled from the hire agreement reference
        self.assertEqual(log.work_order_no, 'RVJ/HA/HI/26-04-50')

    def test_tyre_export_vendor_filter(self):
        TyreLog.objects.create(date=date(2026, 8, 26), vehicle=self.vehicle, vendor='Vendor A', total_amount=100, entered_by=self.deo)
        TyreLog.objects.create(date=date(2026, 8, 27), vehicle=self.vehicle, vendor='Vendor B', total_amount=200, entered_by=self.deo)
        pdf = self.client.get(reverse('fleet:api_export_tyre_pdf'), {'vendor': 'Vendor A'})
        self.assertEqual(pdf.status_code, 200)
        content = pdf.content.decode('utf-8', 'ignore')
        self.assertIn('Vendor A', content)

    def test_vehicle_full_report_excel_and_pdf(self):
        from .models import LubricationLog
        LubricationLog.objects.create(date=date(2026, 8, 26), vehicle=self.vehicle, oil_type='Engine Oil', qty=10, rate=100, amount=1000, total_amount=1000, entered_by=self.deo)
        TyreLog.objects.create(date=date(2026, 8, 26), vehicle=self.vehicle, punctures=1, total_amount=500, entered_by=self.deo)
        excel = self.client.get(reverse('fleet:api_vehicle_report'), {'vehicle_id': self.vehicle.id})
        self.assertEqual(excel.status_code, 200)
        self.assertIn('spreadsheetml', excel['Content-Type'])
        pdf = self.client.get(reverse('fleet:api_vehicle_report_pdf'), {'vehicle_id': self.vehicle.id})
        self.assertEqual(pdf.status_code, 200)
        content = pdf.content.decode('utf-8', 'ignore')
        self.assertIn('Vehicle Complete Report', content)
        self.assertIn('Engine Oil', content)   # lubricants section
        self.assertIn('data:image/png;base64', content)  # logo
