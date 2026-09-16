from django.db import models
from portal.models import CompanyPost
from django.contrib.auth import get_user_model

User = get_user_model()

class Driver(models.Model):
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, blank=True, null=True)
    license_no = models.CharField(max_length=100, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    extra_data = models.JSONField(default=dict, blank=True)
    
    def __str__(self):
        return self.name

class VehicleModel(models.Model):
    name = models.CharField(max_length=150)
    
    def __str__(self):
        return self.name

class FleetVehicle(models.Model):
    department = models.ForeignKey(CompanyPost, on_delete=models.SET_NULL, null=True, blank=True)
    dno = models.CharField(max_length=100, verbose_name="Door No")
    regn = models.CharField(max_length=100, verbose_name="Registration No", blank=True, null=True)
    model_name = models.CharField(max_length=150, blank=True, null=True)
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicles')
    
    # HMR Tracking
    latest_hmr = models.FloatField(default=0.0)
    hmr_date = models.DateField(blank=True, null=True)
    
    # Component Health Tracking (Good, Average, Poor, etc. or remarks)
    engine = models.CharField(max_length=200, blank=True, null=True)
    gearbox = models.CharField(max_length=200, blank=True, null=True)
    brakes = models.CharField(max_length=200, blank=True, null=True)
    steering = models.CharField(max_length=200, blank=True, null=True)
    electricals = models.CharField(max_length=200, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    
    is_active = models.BooleanField(default=True)
    extra_data = models.JSONField(default=dict, blank=True)
    
    def __str__(self):
        return f"{self.dno} - {self.model_name}"


class VehicleMovement(models.Model):
    SHIFT_CHOICES = (
        ('Day', 'Day Shift'),
        ('Night', 'Night Shift'),
    )

    vehicle_number = models.CharField(max_length=150, help_text='Vehicle number (typed)', default='')
    driver_name = models.CharField(max_length=150, help_text='Driver name snapshot retained for history')
    movement_date = models.DateField()
    movement_time = models.TimeField()
    shift = models.CharField(max_length=10, choices=SHIFT_CHOICES)
    destination = models.CharField(max_length=200, blank=True)
    purpose = models.CharField(max_length=255, blank=True)
    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='movements')
    driver = models.ForeignKey('portal.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicle_movements')
    entered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicle_movements_entered')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='movement_deleted')

    class Meta:
        ordering = ('-movement_date', '-movement_time', '-id')

    def __str__(self):
        return f"{self.vehicle_number} - {self.driver_name} - {self.movement_date} {self.movement_time}"

class ServiceLog(models.Model):
    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.CASCADE, related_name='service_logs')
    logged_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    interval = models.CharField(max_length=100, verbose_name="Service Interval (e.g., 250H)")
    last_date = models.DateField()
    current_hmr = models.FloatField(blank=True, null=True)
    last_hmr = models.FloatField(blank=True, null=True)
    next_due = models.FloatField(blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    extra_data = models.JSONField(default=dict, blank=True)
    
    def __str__(self):
        return f"{self.vehicle.dno} - {self.interval}"

class RepairLog(models.Model):
    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.CASCADE, related_name='repair_logs')
    logged_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    
    in_date = models.DateField()
    in_time = models.TimeField(blank=True, null=True)
    
    complaint = models.TextField(blank=True, null=True)
    parts_used = models.TextField(blank=True, null=True)
    qty = models.CharField(max_length=50, blank=True, null=True)
    mechanic = models.CharField(max_length=100, blank=True, null=True)
    
    hmr_at_breakdown = models.FloatField(blank=True, null=True)
    kmr_at_breakdown = models.FloatField(blank=True, null=True)
    
    out_date = models.DateField(blank=True, null=True)
    out_time = models.TimeField(blank=True, null=True)
    
    remarks = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    extra_data = models.JSONField(default=dict, blank=True)
    
    @property
    def status(self):
        if self.out_date:
            return "COMPLETED"
        return "PENDING"


class LubricationLog(models.Model):
    date = models.DateField()
    location = models.CharField(max_length=200, blank=True, null=True)
    work_order_no = models.CharField(max_length=150, blank=True, null=True)
    vendor = models.CharField(max_length=200, blank=True, null=True, help_text='Vendor / Owner name')
    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.CASCADE, related_name='lubrication_logs')
    oil_type = models.CharField(max_length=150)
    qty = models.FloatField()
    unit = models.CharField(max_length=20, default='L')
    rate = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    manpower_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, blank=True, null=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    entered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='lubrication_entered')
    entered_on = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='lubrication_deleted')

    def __str__(self):
        return f"{self.vehicle.regn} - {self.oil_type} on {self.date}"


class TyreLog(models.Model):
    """Tyre puncture / work entry or Store Issued Tyre entry."""
    ENTRY_TYPE_CHOICES = (
        ('PUNCTURE', 'Puncture / Repair'),
        ('ISSUE', 'New Tyre Issued'),
    )
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES, default='PUNCTURE')
    date = models.DateField()
    location = models.CharField(max_length=200, blank=True, null=True)
    work_order_no = models.CharField(max_length=150, blank=True, null=True)
    vendor = models.CharField(max_length=200, blank=True, null=True, help_text='Vendor / Owner name')
    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.CASCADE, related_name='tyre_logs')

    # Puncture Fields
    punctures = models.PositiveIntegerField(default=0)
    big_patches = models.PositiveIntegerField(default=0)
    small_patches = models.PositiveIntegerField(default=0)
    nozzles = models.PositiveIntegerField(default=0)
    valve_pin_number = models.CharField(max_length=150, blank=True, null=True, help_text='Valve Pin Number')
    material_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    big_patches_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    small_patches_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    opening_fitting_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)

    # Store Issued Tyre Fields
    tyre_number = models.CharField(max_length=150, blank=True, null=True, help_text='Tyre Number / Serial No')
    company = models.CharField(max_length=150, blank=True, null=True, help_text='Company / Brand (e.g. MRF, Apollo)')
    ply_number = models.CharField(max_length=50, blank=True, null=True, help_text='Ply number / rating (e.g. 16PR)')
    size_of_tyre = models.CharField(max_length=100, blank=True, null=True, help_text='Size of tyre (e.g. 10.00-20)')
    driver_name = models.CharField(max_length=150, blank=True, null=True, help_text='Driver name')

    entered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tyre_entered')
    entered_on = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tyre_deleted')

    class Meta:
        ordering = ('-date', '-id')

    def __str__(self):
        if self.entry_type == 'ISSUE':
            return f"{self.vehicle.regn} - Tyre Issued ({self.tyre_number or 'No.'}) on {self.date}"
        return f"{self.vehicle.regn} - tyre work on {self.date}"


class HiredVehicle(models.Model):
    """Hired equipment/vehicle master (from Hire Equipments sheet).
    Used to auto-fill vendor (owner), equipment type and agreement no
    when a registration number is selected during entry."""
    regn = models.CharField(max_length=100, unique=True, verbose_name='Registration / Equipment No')
    equipment_type = models.CharField(max_length=200, blank=True, null=True)
    hire_by = models.CharField(max_length=200, blank=True, null=True)
    owner_name = models.CharField(max_length=200, blank=True, null=True, verbose_name='Owner / Vendor')
    agreement_ref = models.CharField(max_length=150, blank=True, null=True, verbose_name='Hire Agreement Reference (WO)')
    contract_status = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=100, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('regn',)

    def __str__(self):
        return f"{self.regn} - {self.owner_name or 'Unknown owner'}"

class SparePart(models.Model):
    part_number = models.CharField(max_length=150, unique=True, help_text='SKU or Part Number')
    part_name = models.CharField(max_length=255)
    category = models.CharField(max_length=150, blank=True, null=True, help_text='Engine, Electrical, Suspension, etc.')
    unit = models.CharField(max_length=50, default='pcs')
    current_stock = models.IntegerField(default=0)
    reorder_level = models.IntegerField(default=5)
    location = models.CharField(max_length=150, blank=True, null=True, help_text='Store Location or Bin Number')
    valuation_rate = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text='Latest valuation rate')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('part_name',)

    def __str__(self):
        return f"{self.part_number} - {self.part_name}"


class SparePartTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('IN', 'Stock In'),
        ('OUT', 'Stock Out'),
    )
    part = models.ForeignKey(SparePart, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    date = models.DateField()
    quantity = models.PositiveIntegerField()
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text='Cost per unit')
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    
    reference_no = models.CharField(max_length=150, blank=True, null=True, help_text='PO No, Invoice No, or WO No')
    
    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='spare_parts_used', help_text='Applicable for Stock Out')
    location = models.CharField(max_length=150, blank=True, null=True, help_text='Location for Stock Out')
    wo_no = models.CharField(max_length=150, blank=True, null=True, help_text='Work Order No')
    manpower_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text='Manpower cost')
    
    mechanic = models.CharField(max_length=150, blank=True, null=True, help_text='Who received the part')
    supplier = models.CharField(max_length=200, blank=True, null=True, help_text='Applicable for Stock In')
    
    remarks = models.TextField(blank=True, null=True)
    entered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_spare_parts_transactions')
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='spare_part_txn_deleted')

    def save(self, *args, **kwargs):
        if self.quantity and self.rate is not None:
            part_cost = self.quantity * self.rate
            mp_cost = self.manpower_cost or 0
            self.total_amount = part_cost + mp_cost
        super().save(*args, **kwargs)

    class Meta:
        ordering = ('-date', '-id')

    def __str__(self):
        return f"{self.transaction_type} - {self.part.part_name} - {self.quantity} on {self.date}"


def _sync_vehicle_memory(vehicle, vendor=None, work_order_no=None, location=None, model_name=None, category=None):
    """Syncs Work Order No and Vendor / Owner into HiredVehicle and vehicle.extra_data"""
    if not vehicle:
        return
    import re
    def _clean(s):
        return re.sub(r'[^A-Za-z0-9]', '', str(s or '')).upper()
        
    reg_clean = _clean(vehicle.regn or vehicle.dno)
    if not reg_clean:
        return
        
    # Update vehicle extra_data
    changed = False
    if not isinstance(vehicle.extra_data, dict):
        vehicle.extra_data = {}
        
    if vendor and str(vendor).strip() and str(vendor).strip() != 'In-House (RIGSAR-VAJRA)':
        vehicle.extra_data['owner_name'] = str(vendor).strip()
        vehicle.extra_data['ownership'] = 'Hired'
        changed = True
        
    if work_order_no and str(work_order_no).strip():
        vehicle.extra_data['contract_ref'] = str(work_order_no).strip()
        changed = True
        
    if location and str(location).strip():
        vehicle.extra_data['location'] = str(location).strip()
        changed = True

    if category and str(category).strip() and str(category).strip() != 'All':
        vehicle.extra_data['category'] = str(category).strip()
        changed = True
        
    if model_name and str(model_name).strip() and (not vehicle.model_name or vehicle.model_name in ('Equipment', 'Vehicle/Equipment')):
        vehicle.model_name = str(model_name).strip()
        changed = True
    elif category and str(category).strip() and (not vehicle.model_name or vehicle.model_name in ('Equipment', 'Vehicle/Equipment')):
        vehicle.model_name = str(category).strip()
        changed = True
        
    if changed:
        vehicle.save()
        
    # Update or create HiredVehicle
    all_hired = list(HiredVehicle.objects.all())
    matched_h = [h for h in all_hired if _clean(h.regn) == reg_clean]
    
    if matched_h:
        hv = matched_h[0]
        hv_changed = False
        if vendor and str(vendor).strip() and str(vendor).strip() != 'In-House (RIGSAR-VAJRA)' and hv.owner_name != str(vendor).strip():
            hv.owner_name = str(vendor).strip()
            hv_changed = True
        if work_order_no and str(work_order_no).strip() and hv.agreement_ref != str(work_order_no).strip():
            hv.agreement_ref = str(work_order_no).strip()
            hv_changed = True
        if model_name and str(model_name).strip() and not hv.equipment_type:
            hv.equipment_type = str(model_name).strip()
            hv_changed = True
        if hv_changed:
            hv.save()
    elif (vendor and str(vendor).strip() and str(vendor).strip() != 'In-House (RIGSAR-VAJRA)') or (work_order_no and str(work_order_no).strip()):
        try:
            HiredVehicle.objects.create(
                regn=vehicle.regn or vehicle.dno,
                owner_name=(str(vendor).strip() if vendor else ''),
                agreement_ref=(str(work_order_no).strip() if work_order_no else ''),
                equipment_type=model_name or category or vehicle.model_name or ''
            )
        except Exception:
            pass


def get_or_create_fleet_vehicle(regn_or_id, vendor=None, work_order_no=None, location=None, model_name=None, category=None):
    """
    Finds or creates a FleetVehicle and keeps HiredVehicle in sync.
    If the vehicle is new, creates it with ownership and extra_data.
    If work_order_no or vendor (owner) is provided, updates HiredVehicle and FleetVehicle.extra_data
    so that future lookups across all sections (Deployment, Lubricants, Tyre, Spare Parts) will autofill.
    """
    import re
    if not regn_or_id:
        return None
    
    # 1. Check if an integer ID or numeric string was passed
    if isinstance(regn_or_id, int) or (isinstance(regn_or_id, str) and regn_or_id.isdigit()):
        try:
            v = FleetVehicle.objects.get(id=int(regn_or_id))
            _sync_vehicle_memory(v, vendor=vendor, work_order_no=work_order_no, location=location, model_name=model_name, category=category)
            return v
        except FleetVehicle.DoesNotExist:
            pass

    # 2. String Registration Number lookup
    regn_str = str(regn_or_id).strip()
    if not regn_str:
        return None
        
    def _clean(s):
        return re.sub(r'[^A-Za-z0-9]', '', str(s or '')).upper()
        
    clean_target = _clean(regn_str)
    if not clean_target:
        return None
        
    # Match by exact or normalized clean regn
    all_vehicles = list(FleetVehicle.objects.all())
    matched = [v for v in all_vehicles if _clean(v.regn) == clean_target or _clean(v.dno) == clean_target]
    
    if matched:
        v = matched[0]
        _sync_vehicle_memory(v, vendor=vendor, work_order_no=work_order_no, location=location, model_name=model_name, category=category)
        return v
        
    # If not found in FleetVehicle, check HiredVehicle
    all_hired = list(HiredVehicle.objects.all())
    matched_h = [h for h in all_hired if _clean(h.regn) == clean_target]
    hv_match = matched_h[0] if matched_h else None
    
    # Create new FleetVehicle
    ownership = 'Hired' if (vendor or hv_match) else 'In-house'
    final_model = model_name or (hv_match.equipment_type if hv_match else (category or 'Vehicle/Equipment'))
    final_vendor = (str(vendor).strip() if vendor else '') or (hv_match.owner_name if hv_match else '')
    final_wo = (str(work_order_no).strip() if work_order_no else '') or (hv_match.agreement_ref if hv_match else '')
    
    v = FleetVehicle.objects.create(
        dno=regn_str,
        regn=regn_str,
        model_name=final_model,
        is_active=True,
        extra_data={
            'ownership': ownership,
            'owner_name': final_vendor,
            'contract_ref': final_wo,
            'location': location or 'Gelephu',
            'category': category or 'Other'
        }
    )
    
    _sync_vehicle_memory(v, vendor=final_vendor, work_order_no=final_wo, location=location, model_name=final_model, category=category)
    return v
