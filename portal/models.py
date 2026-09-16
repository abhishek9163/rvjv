from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from django.conf import settings
import datetime

class CompanyPost(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    
    def __str__(self):
        return self.name

class User(AbstractUser):
    # System roles: DEVELOPER (Superuser), MANAGER (Full Access), EMPLOYEE (Restricted)
    SYSTEM_ROLES = (
        ('MANAGER', 'Manager'),
        ('PROJECT_MANAGER', 'Project Manager (PM)'),
        ('TIME_KEEPER', 'Time Keeper (QR & Field Scanner)'),
        ('DEO', 'DEO (Data Entry Operator)'),
        ('PENDING', 'Pending Approval'),
    )
    system_role = models.CharField(max_length=20, choices=SYSTEM_ROLES, default='PENDING')
    post = models.ForeignKey(CompanyPost, on_delete=models.SET_NULL, null=True, blank=True, related_name='employees')
    employee = models.ForeignKey('Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name='user_accounts', help_text='Linked Employee profile in workforce roster')
    
    otp = models.CharField(max_length=6, blank=True, null=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    profile_picture = models.FileField(upload_to='profiles/', null=True, blank=True)
    notification_prefs = models.JSONField(default=dict, blank=True)
    assigned_modules = models.JSONField(default=list, blank=True)
    captain_category = models.CharField(
        max_length=50,
        default='All',
        blank=True,
        choices=(
            ('All', 'All Departments (Full Access)'),
            ('Scania', 'Scania / Dumper Captain'),
            ('Excavator', 'Excavator Captain'),
            ('Grader', 'Motor Grader Captain'),
            ('Compactor', 'Compactor Captain'),
            ('Other', 'Other Machinery Captain'),
        ),
        help_text='Restricts user to only their assigned machinery department in Daily Deployment'
    )
    enable_social_feed_mode = models.BooleanField(
        default=False,
        verbose_name="Enable Instagram/FB Social Feed Dashboard Mode",
        help_text="Renders dashboard as an interactive social media feed (likes, comments, shares) instead of KPI summary cards."
    )
    last_seen = models.DateTimeField(null=True, blank=True, help_text='Last activity heartbeat (for chat presence)')
    can_view_user_activity = models.BooleanField(default=False, help_text='Designate whether this user can view the User Activity Monitoring section.')

    def is_online(self):
        """Online if a heartbeat was received within the last 60 seconds."""
        if not self.last_seen:
            return False
        from django.utils import timezone
        return (timezone.now() - self.last_seen).total_seconds() < 60

    class Meta:
        verbose_name = "User Account"
        verbose_name_plural = "🔐 Manage Logins & User Access Controls"

    def __str__(self):
        return self.username

class SystemSettings(models.Model):
    trash_retention_days = models.IntegerField(default=30, help_text='Number of days before archived modules are permanently deleted')
    website_name = models.CharField(max_length=100, default='P&M Department')
    logo = models.FileField(upload_to='system/', null=True, blank=True)
    doc_expiry_threshold = models.IntegerField(default=90, help_text='Default warning threshold (in days) for document expiries')
    enable_low_stock_alerts = models.BooleanField(default=True, help_text='Enable alerts for spare parts that fall below reorder level')
    enable_email_notifications = models.BooleanField(default=False, help_text='Enable global email alerts for critical events')
    default_working_shift_hours = models.IntegerField(default=12, help_text='Default working hours for shift rosters')
    
    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(id=1)
        return obj

class Vehicle(models.Model):
    department = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    number = models.CharField(max_length=50)
    image = models.FileField(upload_to='vehicles/', null=True, blank=True)
    driver_name = models.CharField(max_length=100)
    driver_number = models.CharField(max_length=20)
    driver_email = models.EmailField()
    driver_employee_id = models.CharField(max_length=50)
    driver_age = models.IntegerField()
    driver_blood_group = models.CharField(max_length=10)
    
    garage_in_time = models.DateTimeField(null=True, blank=True)
    garage_out_time = models.DateTimeField(null=True, blank=True)
    garage_issue = models.TextField(null=True, blank=True)
    entered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.name} - {self.number}"


class Employee(models.Model):
    emp_id = models.CharField(max_length=50, null=True, blank=True, verbose_name='Employee Registration')
    name = models.CharField(max_length=150, null=True, blank=True)
    nationality = models.CharField(max_length=50)
    designation = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    joining_date = models.DateField(null=True, blank=True)
    work_permit_no = models.CharField(max_length=100, null=True, blank=True)
    work_permit_expiry = models.DateField(null=True, blank=True)
    passport_details = models.CharField(max_length=150, null=True, blank=True)
    contact_info = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(max_length=50, choices=(('Active', 'Active'), ('On Leave', 'On Leave'), ('Terminated', 'Terminated')), default='Active')
    cid_number = models.CharField(max_length=50, null=True, blank=True, verbose_name='CID / National ID')
    account_number = models.CharField(max_length=100, null=True, blank=True, verbose_name='Bank Account Number')
    blood_group = models.CharField(max_length=10, null=True, blank=True, verbose_name='Blood Group')
    overtime_rate = models.FloatField(default=0.0, help_text='Standard Hourly Overtime Rate (Nu/Nu.  per hr)')
    contractor_agency = models.CharField(max_length=150, null=True, blank=True, verbose_name='Contractor / Hiring Agency')
    document_upload = models.FileField(upload_to='employees/docs/', null=True, blank=True)
    document_type = models.CharField(max_length=100, null=True, blank=True, default='General Document')
    current_shift = models.CharField(max_length=50, choices=(('Day', 'Day Shift'), ('Night', 'Night Shift')), null=True, blank=True, default='Day')
    shift_remarks = models.CharField(max_length=255, null=True, blank=True)
    assigned_vehicle = models.ForeignKey('fleet.FleetVehicle', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_employees')
    assigned_mess = models.ForeignKey('MessLocation', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_employees')
    camp_status = models.CharField(
        max_length=20,
        choices=(
            ('INSIDE', '🟢 Inside Camp'),
            ('OUTSIDE', '🔴 Outside / On Duty'),
            ('ON_LEAVE', '🏖️ On Leave'),
        ),
        default='INSIDE'
    )
    camp_room = models.CharField(max_length=100, blank=True, null=True, verbose_name='Camp Room / Block')
    exit_date = models.DateField(null=True, blank=True, verbose_name='Exit / Resignation Date')
    exit_reason = models.TextField(null=True, blank=True, verbose_name='Reason for Leaving')
    created_at = models.DateTimeField(auto_now_add=True)
    entered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='entered_employees')


    def __str__(self):
        return f"{self.emp_id} - {self.name}"

class EmployeeDocument(models.Model):
    DOCUMENT_TYPES = (
        ('PASSPORT', 'Passport'),
        ('WORK_VISA', 'Work Visa / Residence Permit'),
        ('INDIAN_LICENSE', 'Indian HMV License'),
        ('FOREIGN_LICENSE', 'Foreign Host Country License'),
        ('IDP', 'International Driving Permit (IDP)'),
        ('MEDICAL', 'Medical Fitness Certificate'),
        ('CONTRACT', 'Employment Contract'),
        ('PCC', 'Police Clearance Certificate'),
        ('HAZMAT', 'Hazmat/Special Certification'),
        ('OTHER', 'Other License/Permit'),
    )
    
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPES)
    document_number = models.CharField(max_length=100, blank=True, null=True)
    issuing_country = models.CharField(max_length=100, blank=True, null=True)
    issue_date = models.DateField(blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)
    attachment = models.FileField(upload_to='employee_docs/', blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_document_type_display()} - {self.employee.full_name}"

class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(max_length=50, default='SYSTEM')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    link = models.CharField(max_length=200, blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} - {self.title}"

class VehicleDocument(models.Model):
    driver_operator = models.CharField(max_length=150, blank=True, null=True)
    vehicle_type = models.CharField(max_length=100, blank=True, null=True)
    contact_no = models.CharField(max_length=50, blank=True, null=True)
    reports_to = models.CharField(max_length=100, blank=True, null=True)
    registration_no = models.CharField(max_length=100, blank=True, null=True)
    company = models.CharField(max_length=100, blank=True, null=True, default='RVJV')
    chassis_no = models.CharField(max_length=100, blank=True, null=True)
    engine_no = models.CharField(max_length=100, blank=True, null=True)
    rc_issued_on = models.DateField(blank=True, null=True)
    rc_expiry_date = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class InsuranceDocument(models.Model):
    driver_operator = models.CharField(max_length=150, blank=True, null=True, verbose_name="Day Driver / Operator")
    contact_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="Day Contact No.")
    night_driver = models.CharField(max_length=150, blank=True, null=True, verbose_name="Night Driver / Operator")
    night_contact = models.CharField(max_length=50, blank=True, null=True, verbose_name="Night Contact No.")
    vehicle_type = models.CharField(max_length=100, blank=True, null=True, verbose_name="Vehicle / Equipment Type")
    registration_no = models.CharField(max_length=100, blank=True, null=True, verbose_name="Registration No.")
    insurance_provider = models.CharField(max_length=150, blank=True, null=True, verbose_name="Insurance Provider")
    policy_no = models.CharField(max_length=100, blank=True, null=True, verbose_name="Policy No.")
    company_supplier = models.CharField(max_length=100, blank=True, null=True, default='RVJV', verbose_name="Company / Supplier")
    work_site = models.CharField(max_length=150, blank=True, null=True, verbose_name="Work Site / Location")
    category = models.CharField(max_length=100, blank=True, null=True, default='Pool Vehicle', verbose_name="Category")
    reports_to = models.CharField(max_length=100, blank=True, null=True, verbose_name="Reports To")
    issued_on = models.DateField(blank=True, null=True, verbose_name="Issued On")
    expiry_date = models.DateField(blank=True, null=True, verbose_name="Insurance Expiry Date")
    remarks = models.TextField(blank=True, null=True, verbose_name="Remarks / Notes")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.registration_no or 'No Reg'} - {self.vehicle_type or 'Vehicle'} ({self.expiry_date})"


class DailyDeployment(models.Model):
    entered_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='deployments_entered')
    date = models.DateField()
    machinery = models.CharField(max_length=100)
    
    zone_1_2_day = models.IntegerField(default=0)
    zone_1_2_night = models.IntegerField(default=0)
    zone_3_4_day = models.IntegerField(default=0)
    zone_3_4_night = models.IntegerField(default=0)
    borrow_area_day = models.IntegerField(default=0)
    borrow_area_night = models.IntegerField(default=0)
    culvert_area_day = models.IntegerField(default=0)
    culvert_area_night = models.IntegerField(default=0)
    batching_plant_day = models.IntegerField(default=0)
    batching_plant_night = models.IntegerField(default=0)
    crushing_plant_day = models.IntegerField(default=0)
    crushing_plant_night = models.IntegerField(default=0)
    road_maint_day = models.IntegerField(default=0)
    road_maint_night = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_day(self):
        return sum([self.zone_1_2_day, self.zone_3_4_day, self.borrow_area_day, self.culvert_area_day, self.batching_plant_day, self.crushing_plant_day, self.road_maint_day])

    @property
    def total_night(self):
        return sum([self.zone_1_2_night, self.zone_3_4_night, self.borrow_area_night, self.culvert_area_night, self.batching_plant_night, self.crushing_plant_night, self.road_maint_night])
        
    def __str__(self):
        return f"{self.date} - {self.machinery}"


class DailyVehicleAllocation(models.Model):
    """Daily vehicle-to-driver-to-zone allocation entered by department captains."""
    SHIFT_CHOICES = (
        ('Day', 'Day Shift'),
        ('Night', 'Night Shift'),
    )
    CATEGORY_CHOICES = (
        ('BEDDING_LINEN', '🛏️ Bedding, Mattresses & Linen'),
        ('ROOM_FIXTURE', '🏢 Room Fixtures & Electrical'),
        ('KITCHEN_EQUIPMENT', '🍳 Kitchen & Cooking Equipment'),
        ('DINING_UTENSILS', '🍽️ Dining & Utensils'),
        ('CLEANING_HOUSEKEEPING', '🧹 Cleaning & Housekeeping'),
        ('STAFF_UNIFORM_PPE', '🦺 Staff Uniforms & PPE'),
        ('LPG_GAS', '🔥 LPG Gas Cylinders'),
        ('GENERAL_APPLIANCE', '⚡ Electrical & General Appliances'),
        ('OTHER', '📦 General Store & Other Assets'),
    )
    ZONE_CHOICES = (
        ('Zone 1 & 2', 'Zone 1 & 2'),
        ('Zone 3 & 4', 'Zone 3 & 4'),
        ('Borrow Area', 'Borrow Area'),
        ('Culvert Area', 'Culvert Area'),
        ('Batching Plant', 'Batching Plant'),
        ('Crushing Plant', 'Crushing Plant'),
        ('Road Maintenance', 'Road Maintenance'),
        ('Other Location', 'Other Location'),
    )

    date = models.DateField(db_index=True)
    shift = models.CharField(max_length=10, choices=SHIFT_CHOICES, default='Day', db_index=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, db_index=True)
    
    vehicle = models.ForeignKey('fleet.FleetVehicle', on_delete=models.SET_NULL, related_name='daily_allocations', null=True, blank=True)
    vehicle_regn = models.CharField(max_length=100, help_text='Registration No snapshot', db_index=True)
    
    driver = models.ForeignKey('Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name='allocations')
    driver_name = models.CharField(max_length=150, help_text='Driver / Operator Name')
    driver_emp_id = models.CharField(max_length=50, blank=True, null=True, help_text='Driver Employee ID')
    driver_contact = models.CharField(max_length=50, blank=True, null=True)
    
    location_zone = models.CharField(max_length=100, choices=ZONE_CHOICES, default='Zone 1 & 2')
    work_order_no = models.CharField(max_length=150, blank=True, null=True)
    vendor = models.CharField(max_length=200, blank=True, null=True)
    
    out_time = models.CharField(max_length=30, blank=True, null=True, help_text='Out Time / Departure Time e.g. 08:00 AM')
    in_time = models.CharField(max_length=30, blank=True, null=True, help_text='In Time / Return Time e.g. 06:00 PM')
    
    start_hmr = models.FloatField(null=True, blank=True, help_text='Starting HMR/KMR')
    end_hmr = models.FloatField(null=True, blank=True, help_text='Ending HMR/KMR')
    remarks = models.CharField(max_length=255, blank=True, null=True)
    mess_location = models.ForeignKey('MessLocation', on_delete=models.SET_NULL, null=True, blank=True)
    entry_mode = models.CharField(max_length=30, default='SCANNER')
    verification_token = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    
    entered_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicle_allocations_entered')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', 'category', 'vehicle_regn']

    def __str__(self):
        return f"{self.date} [{self.shift}] {self.vehicle_regn} - {self.driver_name} ({self.location_zone})"


class Message(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_messages')
    content = models.TextField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    
    # Rich Messaging Features
    is_edited = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    file = models.FileField(upload_to='chat_attachments/', null=True, blank=True)
    file_type = models.CharField(max_length=20, null=True, blank=True) # 'image', 'audio', 'document'

    def __str__(self):
        content_snippet = self.content[:30] if self.content else f"[File: {self.file_type}]"
        return f"{self.sender} -> {self.receiver}: {content_snippet}"


class UserActivityLog(models.Model):
    ACTION_CHOICES = (
        ('CREATE', 'Created'),
        ('UPDATE', 'Updated'),
        ('DELETE', 'Deleted'),
        ('APPROVE', 'Approved'),
        ('LOGIN', 'Logged In'),
        ('LOGOUT', 'Logged Out'),
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='activity_logs')
    user_name = models.CharField(max_length=150)
    user_role = models.CharField(max_length=50)
    action_type = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    module_name = models.CharField(max_length=100, db_index=True)
    description = models.TextField()
    ip_address = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "User Activity Log"
        verbose_name_plural = "⚡ Live User Activity Logs & Audit Stream"
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.action_type}] {self.user_name} ({self.module_name}) - {self.created_at.strftime('%d %b %Y %H:%M')}"


class OvertimeRecord(models.Model):
    PUNCH_CHOICES = (
        ('IN', 'Punch IN (Shift Start)'),
        ('OUT', 'Punch OUT (Shift End)'),
        ('FULL', 'Direct Completed Entry'),
    )
    STATUS_CHOICES = (
        ('Pending', 'Pending Approval'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    )
    ZONE_CHOICES = (
        ('Zone 1 & 2', 'Zone 1 & 2'),
        ('Crushing Plant', 'Crushing Plant'),
        ('Batching Plant', 'Batching Plant'),
        ('Borrow Area', 'Borrow Area'),
        ('Dam Site', 'Dam Site'),
        ('Power House', 'Power House'),
        ('Workshop / Garage', 'Workshop / Garage'),
        ('Road Maintenance', 'Road Maintenance'),
        ('Other Location', 'Other Location'),
    )
    
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='overtime_records')
    emp_id_snapshot = models.CharField(max_length=50, db_index=True, help_text='Employee Registration / ID Number')
    employee_name = models.CharField(max_length=150, help_text='Employee Full Name')
    designation = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    cid_number = models.CharField(max_length=50, blank=True, null=True, verbose_name='CID / National ID')
    account_number = models.CharField(max_length=100, blank=True, null=True, verbose_name='Bank Account Number')
    contact_info = models.CharField(max_length=100, blank=True, null=True)
    overtime_rate = models.FloatField(default=0.0, help_text='Standard Hourly Rate')
    
    date = models.DateField(db_index=True)
    shift = models.CharField(max_length=20, choices=(('Day', 'Morning / Day Shift'), ('Night', 'Night Shift'), ('General', 'General Shift')), default='Day', db_index=True)
    location_zone = models.CharField(max_length=100, choices=ZONE_CHOICES, default='Zone 1 & 2', db_index=True)
    punch_type = models.CharField(max_length=20, choices=PUNCH_CHOICES, default='IN')
    
    in_time = models.CharField(max_length=30, blank=True, null=True, help_text='Punch IN Time e.g. 08:00 AM')
    out_time = models.CharField(max_length=30, blank=True, null=True, help_text='Punch OUT Time e.g. 05:00 PM')
    
    overtime_hours = models.FloatField(default=0.0, help_text='Total OT Hours computed')
    overtime_amount = models.FloatField(default=0.0, help_text='Total OT Pay (Hours * Rate)')
    
    work_description = models.TextField(blank=True, null=True, help_text='Task / Machine / Activity Assigned')
    vehicle_regn = models.CharField(max_length=100, blank=True, null=True, help_text='Machine / Vehicle attached')
    
    time_keeper = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='overtimes_logged')
    time_keeper_name = models.CharField(max_length=150, blank=True, null=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending', db_index=True)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='overtimes_approved')
    approved_at = models.DateTimeField(null=True, blank=True)
    
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"[{self.date}] {self.emp_id_snapshot} - {self.employee_name} ({self.overtime_hours} hrs)"


class EmployeeAttendance(models.Model):
    STATUS_CHOICES = [
        ('Present', 'Present'),
        ('Absent', 'Absent'),
        ('On Leave', 'On Leave'),
        ('Half Day', 'Half Day'),
        ('Holiday', 'Holiday / Off'),
    ]
    SHIFT_CHOICES = [
        ('General', 'General Shift (8:00 AM - 5:00 PM)'),
        ('Shift A (Morning)', 'Shift A - Morning (6:00 AM - 2:00 PM)'),
        ('Shift B (Evening)', 'Shift B - Evening (2:00 PM - 10:00 PM)'),
        ('Shift C (Night)', 'Shift C - Night (10:00 PM - 6:00 AM)'),
    ]
    PUNCH_SOURCE_CHOICES = [
        ('QR_SCAN', 'QR / Barcode Scan'),
        ('MANUAL', 'Manual Entry / Sheet'),
        ('BULK', 'Bulk Shift Attendance'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField(default=timezone.now)
    shift = models.CharField(max_length=50, default='General', choices=SHIFT_CHOICES)
    status = models.CharField(max_length=20, default='Present', choices=STATUS_CHOICES)
    in_time = models.TimeField(null=True, blank=True)
    out_time = models.TimeField(null=True, blank=True)
    punch_source = models.CharField(max_length=20, default='MANUAL', choices=PUNCH_SOURCE_CHOICES)
    remarks = models.CharField(max_length=255, blank=True, null=True)
    mess_location = models.ForeignKey('MessLocation', on_delete=models.SET_NULL, null=True, blank=True)
    entry_mode = models.CharField(max_length=30, default='SCANNER')
    verification_token = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', 'employee__name']
        constraints = [
            models.UniqueConstraint(fields=['employee', 'date'], name='unique_employee_date_attendance')
        ]

    def __str__(self):
        return f"[{self.date}] {self.employee.name} ({self.employee.emp_id}) - {self.status} [{self.shift}]"




class MessLog(models.Model):
    MEAL_CHOICES = [
        ('BREAKFAST', 'Breakfast'),
        ('LUNCH', 'Lunch'),
        ('SNACKS', 'Evening Tea & Snacks'),
        ('DINNER', 'Dinner'),
    ]
    STATUS_CHOICES = [
        ('SUCCESS', '🟢 Success / Served'),
        ('DUPLICATE', '🔴 Duplicate Punch Blocked'),
        ('INVALID', '⚠️ Invalid Scan'),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='mess_logs')
    date = models.DateField(default=timezone.now, db_index=True)
    meal_type = models.CharField(max_length=20, choices=MEAL_CHOICES, default='LUNCH', db_index=True)
    punch_time = models.DateTimeField(auto_now_add=True)
    scanned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='SUCCESS')
    remarks = models.CharField(max_length=255, blank=True, null=True)
    mess_location = models.ForeignKey('MessLocation', on_delete=models.SET_NULL, null=True, blank=True)
    entry_mode = models.CharField(max_length=30, default='SCANNER')
    verification_token = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    class Meta:
        ordering = ['-punch_time']
        verbose_name = "Mess Meal Log"
        verbose_name_plural = "🍱 Mess Meal Logs"

    def __str__(self):
        return f"[{self.date} {self.meal_type}] {self.employee.name} ({self.employee.emp_id}) - {self.status}"


class MessGuestCoupon(models.Model):
    guest_name = models.CharField(max_length=150)
    contractor_company = models.CharField(max_length=150, blank=True, null=True)
    meal_type = models.CharField(max_length=20, default='LUNCH')
    coupon_code = models.CharField(max_length=50, unique=True)
    is_redeemed = models.BooleanField(default=False)
    redeemed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Mess Guest Coupon"
        verbose_name_plural = "🎟️ Mess Guest Coupons"

    def __str__(self):
        return f"Coupon: {self.coupon_code} - {self.guest_name} ({'Redeemed' if self.is_redeemed else 'Active'})"


class MessMenu(models.Model):
    date = models.DateField(unique=True, default=timezone.now)
    breakfast_menu = models.TextField(blank=True, null=True)
    lunch_menu = models.TextField(blank=True, null=True)
    dinner_menu = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-date']
        verbose_name = "Mess Menu"
        verbose_name_plural = "📋 Mess Menus"

    def __str__(self):
        return f"Mess Menu for {self.date}"


class MessFoodStatus(models.Model):
    MEAL_CHOICES = [
        ('BREAKFAST', '🌅 Breakfast (6 AM - 8 AM)'),
        ('LUNCH', '☀️ Lunch (1 PM - 2 PM)'),
        ('DINNER', '🌙 Dinner (8 PM - 9 PM)'),
    ]
    STATUS_CHOICES = [
        ('CLOSED', '🔴 Kitchen Closed'),
        ('PREPARING', '⏳ Preparing Food'),
        ('READY_IN_5', '⏰ Ready in 5 Minutes!'),
        ('READY', '🟢 Food is READY! Please come'),
    ]
    meal_type = models.CharField(max_length=20, choices=MEAL_CHOICES, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='CLOSED')
    announcement = models.CharField(max_length=255, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Mess Food Status"
        verbose_name_plural = "📢 Mess Food Statuses"

    def __str__(self):
        return f"{self.meal_type} - {self.get_status_display()}"


class MessMealWindow(models.Model):
    SHIFT_CHOICES = [
        ('DAY', '☀️ Day / Morning Shift'),
        ('NIGHT', '🌙 Night Shift'),
        ('ALL', '⭐ All Shifts'),
    ]
    MEAL_CHOICES = [
        ('BREAKFAST', '🌅 Breakfast'),
        ('LUNCH', '☀️ Lunch'),
        ('SNACKS', '☕ Evening Tea & Snacks'),
        ('DINNER', '🌙 Dinner'),
        ('NIGHT_REFRESHMENT', '☕ Night Refreshment / Midnight Meal'),
    ]
    name = models.CharField(max_length=100, help_text="e.g. Day Shift Lunch, Night Refreshment")
    shift = models.CharField(max_length=20, choices=SHIFT_CHOICES, default='DAY')
    meal_type = models.CharField(max_length=30, choices=MEAL_CHOICES, default='LUNCH')
    start_time = models.TimeField(help_text="e.g. 06:00:00")
    end_time = models.TimeField(help_text="e.g. 08:00:00")
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=1)

    class Meta:
        ordering = ['shift', 'display_order', 'start_time']
        verbose_name = "Mess Meal Window Schedule"
        verbose_name_plural = "⏰ Mess Meal Window Schedules"

    def __str__(self):
        return f"[{self.get_shift_display()}] {self.name} ({self.start_time.strftime('%I:%M %p')} - {self.end_time.strftime('%I:%M %p')})"


class MessFeedback(models.Model):
    FEEDBACK_CHOICES = [
        ('DELICIOUS', '😋 Delicious / Bahut Acha'),
        ('GOOD', '👍 Good / Sahi Hai'),
        ('LESS_SALT', '🧂 Needs Less Salt'),
        ('MORE_SALT', '🧂 Needs More Salt'),
        ('HARD_ROTI', '🍞 Roti Hard / Cold'),
        ('CLEANLINESS', '🧹 Cleanliness Issue'),
    ]
    MEAL_CHOICES = [
        ('BREAKFAST', '🌅 Breakfast'),
        ('LUNCH', '☀️ Lunch'),
        ('DINNER', '🌙 Dinner'),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='mess_feedbacks')
    date = models.DateField(default=timezone.now, db_index=True)
    meal_type = models.CharField(max_length=20, choices=MEAL_CHOICES, default='LUNCH')
    feedback_tag = models.CharField(max_length=30, choices=FEEDBACK_CHOICES, default='GOOD')
    comments = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Mess Feedback"
        verbose_name_plural = "💬 Mess Feedbacks"

    def __str__(self):
        return f"{self.employee.name} ({self.date} {self.meal_type}) - {self.feedback_tag}"


class MessLocation(models.Model):
    name = models.CharField(max_length=100, unique=True, help_text="e.g. Mess A (Main Canteen), Mess B (Site 2)")
    code = models.CharField(max_length=50, unique=True, help_text="e.g. MESS_A, MESS_B")
    capacity = models.IntegerField(default=200)
    contractor_agency = models.CharField(max_length=150, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Mess Location"
        verbose_name_plural = "🏢 Mess Locations"

    def __str__(self):
        return self.name


class MessWastageLog(models.Model):
    mess_location = models.ForeignKey(MessLocation, on_delete=models.CASCADE, related_name='wastage_logs', null=True, blank=True)
    date = models.DateField(default=timezone.now, db_index=True)
    meal_type = models.CharField(max_length=20, default='LUNCH')
    prepared_qty_kg = models.FloatField(default=0.0, help_text="Total food prepared in Kg")
    wasted_qty_kg = models.FloatField(default=0.0, help_text="Total food wasted / leftover in Kg")
    contractor_name = models.CharField(max_length=150, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    logged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name = "Mess Wastage Log"
        verbose_name_plural = "🗑️ Mess Food Wastage Logs"

    def __str__(self):
        return f"[{self.date} {self.meal_type}] Wasted {self.wasted_qty_kg} kg"


class CampMovementLog(models.Model):
    DIRECTION_CHOICES = (
        ('IN', '🟢 Entry / In'),
        ('OUT', '🔴 Exit / Out'),
    )
    PURPOSE_CHOICES = (
        ('SHIFT_DUTY', '🚜 Shift / Site Duty'),
        ('PERSONAL', '🛍️ Personal / Market'),
        ('MEDICAL', '🏥 Medical / Clinic'),
        ('LEAVE', '🏖️ Home Leave / Travel'),
        ('OTHER', '📌 Other Reason'),
    )
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='gate_logs')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default='IN', db_index=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    date = models.DateField(default=timezone.now, db_index=True)
    gate_name = models.CharField(max_length=100, default='Main Camp Gate')
    scanned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    entry_mode = models.CharField(max_length=30, default='GUARD_SCAN')
    purpose = models.CharField(max_length=50, choices=PURPOSE_CHOICES, default='SHIFT_DUTY')
    remarks = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Camp Gate Movement Log"
        verbose_name_plural = "🚪 Camp Gate Movement Logs"

    def __str__(self):
        return f"[{self.get_direction_display()}] {self.employee.name} ({self.employee.emp_id}) at {self.timestamp.strftime('%H:%M')}"


class CampSetting(models.Model):
    """
    Singleton / Configuration Model for Camp Geofencing, Curfew Rules & Voice Feedback.
    """
    camp_name = models.CharField(max_length=150, default='Main Site Camp Residence')
    latitude = models.FloatField(default=0.0, help_text="Camp center latitude coordinate")
    longitude = models.FloatField(default=0.0, help_text="Camp center longitude coordinate")
    geofence_radius_meters = models.IntegerField(default=350, help_text="Allowed punch distance radius in meters")
    geofence_enabled = models.BooleanField(default=False, help_text="Enforce GPS location check during self punch")
    
    # Curfew & Alert Settings
    curfew_start_time = models.TimeField(default=datetime.time(21, 30), help_text="Night curfew threshold e.g. 9:30 PM")
    curfew_max_outside_hours = models.IntegerField(default=4, help_text="Max hours allowed outside camp before alert")
    
    # Audio & Voice Settings (Disabled)
    voice_guidance_enabled = models.BooleanField(default=False, help_text="Audio voice greeting setting")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Camp System Setting"
        verbose_name_plural = "⚙️ Camp System Settings"

    def __str__(self):
        return f"{self.camp_name} Settings (Geofence: {'ON' if self.geofence_enabled else 'OFF'})"


class FeedEntryLike(models.Model):
    """
    Social Feed Like Interaction for Movement & Entry Logs across all dashboard modules.
    """
    log_entry = models.ForeignKey(CampMovementLog, on_delete=models.CASCADE, related_name='feed_likes', null=True, blank=True)
    entry_key = models.CharField(max_length=100, db_index=True, blank=True, default='')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='user_feed_likes')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} liked {self.entry_key or self.log_entry_id}"


class FeedEntryComment(models.Model):
    """
    Social Feed Comment Interaction for Movement & Entry Logs across all dashboard modules.
    """
    log_entry = models.ForeignKey(CampMovementLog, on_delete=models.CASCADE, related_name='feed_comments', null=True, blank=True)
    entry_key = models.CharField(max_length=100, db_index=True, blank=True, default='')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='user_feed_comments')
    comment_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user.username} on {self.entry_key or self.log_entry_id}: {self.comment_text[:20]}"


class CampBlock(models.Model):
    block_code = models.CharField(max_length=50, unique=True, help_text="e.g. Block A, Block B ... Block P")
    name = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=50, choices=(('NATIONAL', '🇧🇹 National / Bhutanese'), ('EXPATRIATE', '🇮🇳 Expatriate / Indian'), ('MIXED', '🌐 Mixed')), default='MIXED')
    total_rooms = models.IntegerField(default=20)
    capacity = models.IntegerField(default=160)
    caretaker_name = models.CharField(max_length=100, blank=True, null=True)
    caretaker_contact = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        verbose_name = "Camp Block"
        verbose_name_plural = "🏢 Camp Blocks (A-P)"

    def __str__(self):
        return self.block_code


class CampRoom(models.Model):
    KEY_STATUS_CHOICES = (
        ('ISSUED', '🔑 Key Issued to Resident'),
        ('IN_GATE_BOX', '🔏 Key in Gate Box'),
        ('LOST', '⚠️ Key Lost / Missing'),
    )
    block = models.ForeignKey(CampBlock, on_delete=models.CASCADE, related_name='rooms', null=True, blank=True)
    room_number = models.CharField(max_length=50, unique=True, help_text="e.g. P1, P2, O17, A3")
    capacity = models.IntegerField(default=8)
    room_key_number = models.CharField(max_length=50, blank=True, null=True, help_text="e.g. KEY-P1")
    key_status = models.CharField(max_length=30, choices=KEY_STATUS_CHOICES, default='ISSUED')
    key_issued_to = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='held_room_keys')
    
    # Room-level Fixed Fixtures (Allotted to Room)
    fan_count = models.IntegerField(default=1, help_text="Number of ceiling/wall fans in room")
    fan_status = models.CharField(max_length=30, choices=(('WORKING', '✅ Working'), ('FAULTY', '⚠️ Faulty / Repair Needed'), ('NONE', '❌ None')), default='WORKING')
    tubelight_count = models.IntegerField(default=2, help_text="Number of tubelights/bulbs in room")
    tubelight_status = models.CharField(max_length=30, choices=(('WORKING', '✅ Working'), ('FAULTY', '⚠️ Faulty / Repair Needed'), ('NONE', '❌ None')), default='WORKING')
    door_lock_status = models.CharField(max_length=30, choices=(('OK', '✅ Door Lock OK'), ('REPAIR_NEEDED', '⚠️ Lock Damaged / Loose')), default='OK')

    class Meta:
        verbose_name = "Camp Room"
        verbose_name_plural = "🚪 Camp Rooms & Fixtures"

    def __str__(self):
        return f"{self.room_number} ({self.block.block_code if self.block else 'Camp'})"


class CampAssetAllotment(models.Model):
    CONDITION_CHOICES = (
        ('GOOD', '✅ Good Condition'),
        ('DAMAGED', '⚠️ Damaged'),
        ('RETURNED', '📦 Returned'),
    )
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name='allotted_camp_assets')
    room = models.ForeignKey(CampRoom, on_delete=models.SET_NULL, null=True, blank=True, related_name='resident_assets')
    bed_number = models.CharField(max_length=30, blank=True, null=True, help_text="e.g. Bed 1, Bed 2")
    bed_cot_allotted = models.BooleanField(default=True)
    mattress_allotted = models.BooleanField(default=True)
    mattress_code = models.CharField(max_length=50, blank=True, null=True)
    pillow_allotted = models.BooleanField(default=True)
    fan_allotted = models.BooleanField(default=True)
    blanket_allotted = models.BooleanField(default=True)
    bucket_mug_issued = models.BooleanField(default=True)
    locker_key_issued = models.BooleanField(default=False)
    locker_number = models.CharField(max_length=50, blank=True, null=True)
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='GOOD')
    issue_date = models.DateField(default=timezone.now)

    class Meta:
        verbose_name = "Person Inventory Allotment"
        verbose_name_plural = "🛋️ Person Asset Allotments (Bed, Mattress, Pillow)"

    def __str__(self):
        return f"{self.employee.name} - Room {self.room.room_number if self.room else 'N/A'}"


class MessScanLog(models.Model):
    MEAL_TYPES = (
        ('BREAKFAST', '🌅 Breakfast'),
        ('LUNCH', '☀️ Lunch'),
        ('DINNER', '🌙 Dinner'),
    )
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='mess_scan_logs')
    mess_location = models.ForeignKey(MessLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name='scan_logs')
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPES, default='LUNCH')
    scan_timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    date = models.DateField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=30, default='ALLOWED')

    class Meta:
        ordering = ['-scan_timestamp']
        verbose_name = "Mess Meal Scan Log"
        verbose_name_plural = "🍱 Mess Meal Scan Logs"

    def __str__(self):
        return f"{self.employee.name} scanned at {self.mess_location} ({self.date} {self.meal_type})"


# ==============================================================================
# LEAVE MANAGEMENT & EMPLOYEE LEAVE RECORDS
# ==============================================================================
class EmployeeLeaveRecord(models.Model):
    LEAVE_TYPE_CHOICES = (
        ('LABOUR_BATCH', '👷 Labour Multi-MSW'),
        ('COMPANY_STAFF', '🏢 Company Staff'),
    )
    STATUS_CHOICES = (
        ('ACTIVE_ON_LEAVE', '🟡 Active On Leave'),
        ('RETURNED', '✅ Returned / Rejoined'),
        ('CANCELLED', '❌ Cancelled'),
        ('EXTENDED', '⏳ Extended'),
    )

    form_number = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    leave_type = models.CharField(max_length=30, choices=LEAVE_TYPE_CHOICES, default='LABOUR_BATCH')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_records')
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    total_days = models.IntegerField(default=1)
    leave_category = models.CharField(max_length=50, blank=True, null=True, default='Home Leave')
    reason = models.TextField(blank=True, null=True)
    destination = models.CharField(max_length=200, blank=True, null=True)
    contact_number = models.CharField(max_length=50, blank=True, null=True)
    replacement_worker = models.CharField(max_length=150, blank=True, null=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='ACTIVE_ON_LEAVE', db_index=True)
    actual_return_date = models.DateField(blank=True, null=True)
    approved_by = models.CharField(max_length=100, blank=True, null=True, default='Manager Sir / HR')
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Employee Leave Record"
        verbose_name_plural = "📋 Employee Leave Records"

    def __str__(self):
        return f"{self.form_number or 'LV'} - {self.employee.name} ({self.start_date} to {self.end_date})"


# ==============================================================================
# MESS & OPERATIONAL ASSET MANAGEMENT
# ==============================================================================
class MessAssetItem(models.Model):
    CATEGORY_CHOICES = (
        ('KITCHEN_EQUIPMENT', '🍳 Kitchen & Cooking Equipment'),
        ('DINING_UTENSILS', '🍽️ Dining & Utensils'),
        ('CLEANING_HOUSEKEEPING', '🧹 Cleaning & Housekeeping'),
        ('STAFF_UNIFORM_PPE', '🦺 Staff Uniforms & PPE'),
        ('LPG_GAS', '🔥 LPG Gas Cylinders'),
        ('GENERAL_APPLIANCE', '🔌 Electrical & General Appliances'),
    )
    CONDITION_CHOICES = (
        ('GOOD', '✅ Good Condition'),
        ('FAIR', '🟡 Fair / Usable'),
        ('REPAIR_NEEDED', '⚠️ Repair Needed'),
        ('CONDEMNED', '❌ Damaged / Condemned'),
    )

    name = models.CharField(max_length=150, db_index=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='DINING_UTENSILS')
    asset_code = models.CharField(max_length=50, blank=True, null=True, unique=True)
    mess_location = models.ForeignKey(MessLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name='assets')
    total_quantity = models.IntegerField(default=1)
    available_quantity = models.IntegerField(default=1)
    unit = models.CharField(max_length=30, default='Pcs') # Pcs, Sets, Cylinders, Boxes
    condition_status = models.CharField(max_length=30, choices=CONDITION_CHOICES, default='GOOD')
    purchase_date = models.DateField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = "Mess Asset Item"
        verbose_name_plural = "🍳 Mess Asset Items (Inventory)"

    def __str__(self):
        return f"{self.name} ({self.total_quantity} {self.unit})"

    @property
    def issued_quantity(self):
        return max(0, (self.total_quantity or 0) - (self.available_quantity or 0))


class MessAssetAllocation(models.Model):
    ALLOCATION_TYPE_CHOICES = (
        ('ROOM', '🏢 Room / Barrack'),
        ('RESIDENT', '👤 Room Resident / Individual Worker'),
        ('MESS_LOCATION', '🍳 Mess Facility / Section'),
        ('STAFF_MEMBER', '👨‍🍳 Mess Staff (Cook / Cleaner / Helper)'),
        ('OTHER_SITE', '🚚 Other Site / External Transfer'),
    )
    STATUS_CHOICES = (
        ('ISSUED', 'In-Use / Issued'),
        ('PARTIALLY_RETURNED', 'Partially Returned'),
        ('RETURNED', 'Returned'),
        ('LOST_DAMAGED', 'Lost / Damaged'),
    )

    asset = models.ForeignKey(MessAssetItem, on_delete=models.CASCADE, related_name='allocations')
    allocated_to_type = models.CharField(max_length=30, choices=ALLOCATION_TYPE_CHOICES, default='RESIDENT', db_index=True)
    room = models.ForeignKey(CampRoom, on_delete=models.SET_NULL, null=True, blank=True, related_name='room_asset_allocations')
    room_number = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    staff_member = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='mess_asset_allocations')
    staff_name = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    staff_role = models.CharField(max_length=50, blank=True, null=True) # Cook, Cleaner, Helper, Mess Incharge, Resident
    mess_location = models.ForeignKey(MessLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name='location_asset_allocations')
    location_name = models.CharField(max_length=150, blank=True, null=True, db_index=True)

    # Transfer to Other Sites / External Location Details
    vehicle_number = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    handover_to = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    handover_phone = models.CharField(max_length=50, blank=True, null=True)
    gate_pass_no = models.CharField(max_length=100, blank=True, null=True)

    quantity = models.IntegerField(default=1)
    issue_date = models.DateField(default=timezone.now)
    expected_return_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='ISSUED', db_index=True)
    condition_on_issue = models.CharField(max_length=50, default='Good Condition')
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Mess Asset Allocation"
        verbose_name_plural = "📤 Mess Asset Allocations (Issuances)"

    @property
    def recipient_name(self):
        return self.handover_to or self.staff_name or (self.staff_member.name if self.staff_member else 'N/A')

    @property
    def location_display(self):
        return self.location_name or (self.mess_location.name if self.mess_location else (f"Room {self.room_number}" if self.room_number else 'Main Camp Mess'))

    @property
    def target_display(self):
        if self.allocated_to_type == 'ROOM':
            return f"Room {self.room_number or (self.room.room_number if self.room else 'N/A')}"
        elif self.allocated_to_type == 'RESIDENT':
            worker = self.staff_name or (self.staff_member.name if self.staff_member else 'Resident')
            r_num = self.room_number or (self.staff_member.camp_room if self.staff_member else '')
            return f"{worker} (Room {r_num or 'N/A'})"
        elif self.allocated_to_type == 'OTHER_SITE':
            dest = self.location_name or 'External Site'
            person = self.handover_to or self.staff_name or ''
            veh = f" [Veh: {self.vehicle_number}]" if self.vehicle_number else ''
            if person:
                return f"🚚 {dest} (Handover: {person}){veh}"
            return f"🚚 {dest}{veh}"
        else:
            person = self.staff_name or (self.staff_member.name if self.staff_member else '')
            loc = self.location_name or (self.mess_location.name if self.mess_location else '')
            role = self.staff_role or ''
            if person and loc:
                return f"{person} ({role}) @ {loc}" if role else f"{person} @ {loc}"
            elif person:
                return f"{person} ({role})" if role else person
            elif loc:
                return f"{loc} ({role})" if role else loc
            return "Mess Section"

    def __str__(self):
        return f"{self.quantity}x {self.asset.name} to {self.target_display} ({self.status})"


class MessAssetReturnLog(models.Model):
    CONDITION_CHOICES = (
        ('GOOD', '✅ Good / Reusable'),
        ('DAMAGED_REPAIRABLE', '⚠️ Damaged (Repairable)'),
        ('SCRAPPED', '❌ Broken / Scrapped'),
        ('LOST', '❓ Lost / Missing'),
    )

    allocation = models.ForeignKey(MessAssetAllocation, on_delete=models.CASCADE, related_name='return_logs')
    return_date = models.DateField(default=timezone.now)
    returned_quantity = models.IntegerField(default=1)
    condition = models.CharField(max_length=30, choices=CONDITION_CHOICES, default='GOOD')
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Mess Asset Return Log"
        verbose_name_plural = "📥 Mess Asset Return Logs"

    @property
    def target_display(self):
        return self.allocation.target_display if self.allocation else 'N/A'

    @property
    def received_by_display(self):
        if self.received_by:
            return self.received_by.get_full_name() or self.received_by.username
        return 'Admin'

    def __str__(self):
        return f"{self.returned_quantity}x {self.allocation.asset.name} returned ({self.condition})"


# ==============================================================================
# SAFETY DEPARTMENT: STORE, EQUIPMENT ISSUANCE, RETURNS & FINES
# ==============================================================================

class SafetyStoreItem(models.Model):
    CATEGORY_CHOICES = (
        ('HELMET', 'Safety Helmet / Hard Hat'),
        ('SHOES', 'Safety Shoes / Steel-Toe Boots'),
        ('VEST', 'High-Visibility Safety Vest'),
        ('GLOVES', 'Protective Hand Gloves'),
        ('HARNESS', 'Full Body Safety Harness / Lanyard'),
        ('GOGGLES', 'Eye Protection Goggles / Visor'),
        ('EARPLUG', 'Ear Plugs / Hearing Protection'),
        ('RESPIRATOR', 'Dust Mask / Chemical Respirator'),
        ('OTHER', 'Other Safety Equipment'),
    )

    item_code = models.CharField(max_length=50, unique=True, verbose_name="Item Code / SKU", help_text="e.g. SAF-HLM-001")
    name = models.CharField(max_length=150, verbose_name="Equipment Name")
    category = models.CharField(max_length=100, default='General PPE', help_text="Category name, e.g. Head Protection, Shoes, Reflectors, etc.")
    unit = models.CharField(max_length=30, default='Pairs', help_text="e.g. Pairs, Pcs, Sets")
    specification = models.CharField(max_length=255, blank=True, null=True, help_text="Brand, Model, Size range, standard certifications")
    
    total_stock = models.IntegerField(default=0, verbose_name="Total Stock Purchased")
    available_stock = models.IntegerField(default=0, verbose_name="Current Stock Available")
    minimum_alert_level = models.IntegerField(default=5, verbose_name="Minimum Threshold Alert")
    
    warranty_months = models.IntegerField(default=6, verbose_name="Warranty Period (Months)", help_text="Standard replacement cycle in months")
    fine_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name="Full Cost / Replacement Value (Nu)", help_text="Standard full cost used for prorated fine calculation (Cost / Life * Remaining Months)")
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='safety_items_created')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = "Safety Store Item"
        verbose_name_plural = "🦺 Safety Store Items"

    @property
    def issued_stock(self):
        return max(0, self.total_stock - self.available_stock)

    @property
    def is_low_stock(self):
        return self.available_stock <= self.minimum_alert_level

    def __str__(self):
        return f"{self.name} ({self.item_code}) - Stock: {self.available_stock} {self.unit}"


class SafetyEquipmentIssue(models.Model):
    STATUS_CHOICES = (
        ('ACTIVE', 'Active (In Use)'),
        ('REPLACED_NORMAL', 'Replaced (Normal Expired)'),
        ('REPLACED_PREMATURE', 'Replaced (Premature Damage/Loss)'),
        ('RETURNED_RESIGNED', 'Returned (Employee Resigned)'),
        ('VOID', 'Cancelled / Void'),
    )

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='safety_equipment_issues')
    item = models.ForeignKey(SafetyStoreItem, on_delete=models.PROTECT, related_name='issue_records')
    quantity = models.IntegerField(default=1)
    size_specification = models.CharField(max_length=50, blank=True, null=True, help_text="Size/Spec allocated, e.g. 8, 9, L, XL")
    issue_date = models.DateField(default=timezone.now)
    warranty_expiry_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='ACTIVE')
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='safety_issues_given')
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-issue_date', '-created_at']
        verbose_name = "Safety Equipment Issue"
        verbose_name_plural = "👷 Safety Equipment Issues"

    def save(self, *args, **kwargs):
        if not self.warranty_expiry_date and self.item:
            months = self.item.warranty_months or 6
            base_date = self.issue_date or timezone.now().date()
            self.warranty_expiry_date = base_date + datetime.timedelta(days=int(months * 30))
        super().save(*args, **kwargs)

    @property
    def is_under_warranty(self):
        if not self.warranty_expiry_date:
            return False
        today = timezone.now().date()
        return today <= self.warranty_expiry_date

    @property
    def days_remaining(self):
        if not self.warranty_expiry_date:
            return 0
        today = timezone.now().date()
        diff = (self.warranty_expiry_date - today).days
        return max(0, diff)

    @property
    def days_used(self):
        if not self.issue_date:
            return 0
        today = timezone.now().date()
        diff = (today - self.issue_date).days
        return max(0, diff)

    def __str__(self):
        return f"{self.employee.name if self.employee else 'Worker'} - {self.item.name if self.item else 'Item'} ({self.status})"


class SafetyReplacementAndFine(models.Model):
    REASON_CHOICES = (
        ('WORN_OUT_NORMAL', 'Normal Wear & Tear (Warranty Expired)'),
        ('DAMAGED_PREMATURE', 'Premature Damage / Torn Before Warranty'),
        ('LOST_BY_WORKER', 'Lost / Misplaced by Employee'),
        ('SIZE_EXCHANGE', 'Size Exchange (Good / Unused Condition)'),
        ('OTHER', 'Other Reason'),
    )

    FINE_STATUS_CHOICES = (
        ('NO_FINE', 'No Fine (Normal / Exempted)'),
        ('PENDING_APPROVAL', 'Pending Manager Approval'),
        ('APPROVED_SALARY_DEDUCTION', 'Approved for Salary Deduction'),
        ('PAID_CASH', 'Paid in Cash / Resolved'),
        ('WAIVED', 'Waived / Excused by Manager'),
    )

    original_issue = models.ForeignKey(SafetyEquipmentIssue, on_delete=models.CASCADE, related_name='replacements_made')
    new_issue = models.ForeignKey(SafetyEquipmentIssue, on_delete=models.SET_NULL, null=True, blank=True, related_name='replaced_from_record')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='safety_replacements_and_fines')
    item = models.ForeignKey(SafetyStoreItem, on_delete=models.PROTECT, related_name='replacement_incidents')
    
    replacement_date = models.DateField(default=timezone.now)
    reason = models.CharField(max_length=30, choices=REASON_CHOICES, default='DAMAGED_PREMATURE')
    old_item_condition = models.CharField(max_length=150, blank=True, null=True, help_text="e.g. Sole separated, strap torn, lost")
    is_premature = models.BooleanField(default=False)
    days_used = models.IntegerField(default=0)
    warranty_days = models.IntegerField(default=0)
    months_used = models.IntegerField(default=0, help_text="Total months completed before replacement")
    remaining_months = models.IntegerField(default=0, help_text="Warranty months remaining when replaced")
    full_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Total item cost used in formula (Cost / Life * Remaining Months)")
    
    fine_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    fine_status = models.CharField(max_length=30, choices=FINE_STATUS_CHOICES, default='PENDING_APPROVAL')
    waived_reason = models.TextField(blank=True, null=True)
    
    processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='safety_replacements_handled')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-replacement_date', '-created_at']
        verbose_name = "Safety Replacement & Fine"
        verbose_name_plural = "⚖️ Safety Replacements & Fines"

    def __str__(self):
        emp_name = self.employee.name if self.employee else 'Worker'
        item_name = self.item.name if self.item else 'Item'
        return f"Replacement: {emp_name} - {item_name} (Fine: {self.fine_amount}, {self.fine_status})"


# ==============================================================================
# MANPOWER MANAGEMENT MODELS
# ==============================================================================

class LabourRecord(models.Model):
    """Labour Register — from Labour man power List.xlsx"""
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Leave', 'On Leave'),
        ('Left', 'Left'),
        ('Registered', 'Registered'),
        ('Transferred', 'Transferred'),
    ]
    NATIONALITY_CHOICES = [
        ('Bhutanese', 'Bhutanese'),
        ('Indian', 'Indian'),
        ('Other', 'Other'),
    ]

    labour_id     = models.CharField(max_length=50, unique=True, db_index=True)
    name          = models.CharField(max_length=200)
    gender        = models.CharField(max_length=20, blank=True, null=True)
    nationality   = models.CharField(max_length=50, choices=NATIONALITY_CHOICES, default='Indian')
    cid_number    = models.CharField(max_length=50, blank=True, null=True)
    voter_id      = models.CharField(max_length=50, blank=True, null=True)
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = "Labour Record"
        verbose_name_plural = "👷 Labour Register"

    def __str__(self):
        return f"{self.name} ({self.labour_id}) — {self.status}"


class RVJVEmployee(models.Model):
    """RVJV Employee Register — from Man power of rvjv.xlsx"""
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Inactive', 'Inactive'),
        ('Left', 'Left'),
    ]
    NATIONALITY_CHOICES = [
        ('Bhutanese', 'Bhutanese'),
        ('Indian', 'Indian'),
        ('Other', 'Other'),
    ]

    emp_id          = models.CharField(max_length=50, unique=True, db_index=True)
    name            = models.CharField(max_length=200)
    designation     = models.CharField(max_length=200, blank=True, null=True)
    department      = models.CharField(max_length=200, blank=True, null=True)
    nationality     = models.CharField(max_length=50, choices=NATIONALITY_CHOICES, default='Bhutanese')
    date_of_joining = models.DateField(blank=True, null=True)
    work_permit     = models.CharField(max_length=100, blank=True, null=True)
    cid_number      = models.CharField(max_length=50, blank=True, null=True)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')
    relieving_date  = models.DateField(blank=True, null=True)
    mobile          = models.CharField(max_length=30, blank=True, null=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['department', 'name']
        verbose_name = "RVJV Employee"
        verbose_name_plural = "🏢 RVJV Employee Register"

    def __str__(self):
        return f"{self.name} ({self.emp_id}) — {self.department or 'N/A'}"


class HiredOperator(models.Model):
    """Hired Vehicles & Operators — from Hiring Man power List.xlsx"""
    HIRE_TYPE_CHOICES = [
        ('Driver/Operator', 'Driver / Operator'),
        ('Mechanic/Supervisor', 'Mechanic / Supervisor'),
        ('Other', 'Other'),
    ]
    NATIONALITY_CHOICES = [
        ('Bhutanese', 'Bhutanese'),
        ('Indian', 'Indian'),
        ('Other', 'Other'),
    ]

    operator_id     = models.CharField(max_length=50, unique=True, db_index=True)
    name            = models.CharField(max_length=200)
    vehicle_no      = models.CharField(max_length=50, blank=True, null=True)
    hire_agent      = models.CharField(max_length=200, blank=True, null=True)
    cid_no          = models.CharField(max_length=50, blank=True, null=True)
    hire_type       = models.CharField(max_length=50, choices=HIRE_TYPE_CHOICES, default='Driver/Operator')
    designation     = models.CharField(max_length=200, blank=True, null=True)
    nationality     = models.CharField(max_length=50, choices=NATIONALITY_CHOICES, default='Bhutanese')
    date_of_joining = models.DateField(blank=True, null=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['hire_agent', 'name']
        verbose_name = "Hired Operator"
        verbose_name_plural = "🔧 Hired Vehicles & Operators"

    def __str__(self):
        return f"{self.name} ({self.operator_id}) — {self.hire_agent or 'N/A'}"


class ContractorWorkerPPE(models.Model):
    """Contractor Worker PPE Issuances — from HIRINGS.xlsx (all sheets merged)"""

    worker_id       = models.CharField(max_length=50, db_index=True)
    name            = models.CharField(max_length=200)
    contractor_name = models.CharField(max_length=200, db_index=True)
    designation     = models.CharField(max_length=200, blank=True, null=True)

    # PPE Items (raw cell text, e.g. "1 (7) 9/1/26")
    reflector       = models.CharField(max_length=500, blank=True, null=True)
    safety_boot     = models.CharField(max_length=500, blank=True, null=True)
    helmet          = models.CharField(max_length=500, blank=True, null=True)
    gloves          = models.CharField(max_length=500, blank=True, null=True)
    face_mask       = models.CharField(max_length=500, blank=True, null=True)
    safety_goggles  = models.CharField(max_length=500, blank=True, null=True)
    ear_plug        = models.CharField(max_length=500, blank=True, null=True)
    shoulder_pads   = models.CharField(max_length=500, blank=True, null=True)
    body_harness    = models.CharField(max_length=500, blank=True, null=True)
    raincoat        = models.CharField(max_length=500, blank=True, null=True)
    # Extra PPE stored as JSON {column_name: cell_value}
    extra_ppe_json  = models.JSONField(default=dict, blank=True)

    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        # Unique per worker+contractor combination
        unique_together = [('worker_id', 'contractor_name')]
        ordering = ['contractor_name', 'name']
        verbose_name = "Contractor Worker PPE"
        verbose_name_plural = "🦺 Contractor Worker PPE Issuances"

    def __str__(self):
        return f"{self.name} ({self.worker_id}) @ {self.contractor_name}"
