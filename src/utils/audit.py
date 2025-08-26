"""
Admin action auditing helper module.
Provides best-effort logging of admin actions to the admin_logs table.

Usage:
    from src.utils.audit import log_admin_action, log_admin_action_auto
    
    # Manual logging with explicit admin
    log_admin_action("admin@example.com", "CreateUser", "Created user with email user@example.com", request)
    
    # Automatic logging that extracts current admin from request context
    log_admin_action_auto("UpdateRole", "Updated user role to admin")

Features:
    - Best-effort: Never raises exceptions to avoid disrupting main request flow
    - Automatic IP and User-Agent enrichment when request object is provided
    - Input validation and truncation for safe database storage
    - Supports both manual and automatic admin context extraction

Instrumented routes:
    - Admin login (/api/admin/login)
    - Admin logout (/api/admin/logout) 
    - Client login (/api/auth/login) - for admin users only
    - Restaurant updates (/api/admin/restaurants/<id>)
    - Logs listing (/api/logs)
    - Logs export (/api/logs/export)
"""
import logging
from typing import Optional, Tuple
from datetime import datetime
from flask import Request, request

logger = logging.getLogger(__name__)

def get_current_admin() -> Optional[str]:
    """
    Extract current admin email/identifier from the request context.
    
    Returns:
        Admin email if available, None otherwise
    """
    try:
        # Import here to avoid circular dependency
        from .helpers import get_user_info
        
        user_info = get_user_info()
        if user_info and user_info.get('email'):
            return user_info['email']
            
        return None
    except Exception as e:
        logger.warning(f"Failed to get current admin context: {e}")
        return None

def log_admin_action(admin: str, action: str, details: str, request: Optional[Request] = None) -> None:
    """
    Best-effort logging of admin actions to the admin_logs table.
    
    Args:
        admin: Admin email/identifier
        action: Short action verb (e.g., "Login", "CreateUser", "UpdateRole")
        details: Concise summary of the action
        request: Optional Flask request object for IP/UA enrichment
    
    Returns:
        None - This function never raises exceptions to avoid disrupting main flow
    """
    try:
        # Import here to avoid circular dependency
        from .helpers import supabase, supabase_service
        
        # Prefer service role client for audit logging (bypasses RLS)
        client = supabase_service or supabase
        if not client:
            logger.warning("Audit logging skipped: Supabase client not available")
            return
            
        # Validate inputs
        if not admin or not admin.strip():
            logger.warning("Audit logging skipped: Empty admin identifier")
            return
            
        if not action or not action.strip():
            logger.warning("Audit logging skipped: Empty action")
            return
            
        if not details or not details.strip():
            logger.warning("Audit logging skipped: Empty details")
            return
            
        # Clean and truncate inputs
        admin = admin.strip()[:255]  # Reasonable limit for admin field
        action = action.strip()[:100]  # Reasonable limit for action field
        details = details.strip()
        
        # Enrich details with request information if provided
        if request:
            try:
                ip_address = request.remote_addr or request.environ.get('REMOTE_ADDR', 'unknown')
                user_agent = request.headers.get('User-Agent', 'unknown')[:500]  # Limit UA length
                
                # Append IP and UA to details
                details += f" | ip={ip_address} ua={user_agent[:100]}..."  # Truncate UA in display
            except Exception as e:
                logger.warning(f"Failed to enrich audit details with request info: {e}")
        
        # Truncate details to safe length (16KB as mentioned in requirements)
        max_details_length = 16 * 1024  # 16KB
        if len(details) > max_details_length:
            details = details[:max_details_length - 3] + "..."
            
        # Prepare log entry
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "admin": admin,
            "action": action,
            "details": details
        }
        
        # Insert into admin_logs table
        result = client.table("admin_logs").insert(log_entry).execute()
        
        if result.data:
            logger.info(f"Admin action logged: {action} by {admin}")
        else:
            logger.warning(f"Failed to log admin action: {action} by {admin} - no data returned")
            
    except Exception as e:
        # Best-effort: never raise, just log the failure
        logger.warning(f"Failed to log admin action ({action} by {admin}): {e}")

def log_admin_action_auto(action: str, details: str, request_obj: Optional[Request] = None) -> None:
    """
    Convenience function that automatically gets current admin and logs the action.
    
    Args:
        action: Short action verb (e.g., "Login", "CreateUser", "UpdateRole")
        details: Concise summary of the action
        request_obj: Optional Flask request object, defaults to current request
    """
    try:
        admin = get_current_admin()
        if not admin:
            # Skip logging if no admin context available (best-effort)
            return
            
        # Use provided request or default to current request
        req = request_obj if request_obj is not None else request
        
        log_admin_action(admin, action, details, req)
    except Exception as e:
        logger.warning(f"Failed to auto-log admin action ({action}): {e}")