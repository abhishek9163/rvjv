import logging
from .models import UserActivityLog

logger = logging.getLogger(__name__)

def log_activity(user, action_type, module_name, description, request=None):
    """
    Safely record a user activity log without interrupting main request flow.
    """
    try:
        if not user or not getattr(user, 'is_authenticated', False):
            return None
        
        user_name = getattr(user, 'full_name', '') or user.username
        user_role = getattr(user, 'system_role', 'User') or 'User'
        if getattr(user, 'is_superuser', False):
            user_role = 'Superuser'
            
        ip_addr = None
        if request:
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                ip_addr = x_forwarded_for.split(',')[0].strip()
            else:
                ip_addr = request.META.get('REMOTE_ADDR')

        return UserActivityLog.objects.create(
            user=user,
            user_name=user_name,
            user_role=user_role,
            action_type=action_type,
            module_name=module_name,
            description=description,
            ip_address=ip_addr
        )
    except Exception as e:
        logger.error(f"Failed to record UserActivityLog: {e}")
        return None
