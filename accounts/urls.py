from django.urls import path
from .views import (
    signup_view,
    login_view,
    logout_view,
    totp_setup_view,
    totp_verify_view,
    api_user_info,
    api_login,
    api_signup,
    api_totp_setup,
    api_totp_verify,
    api_logout,
    api_admin_clients,
    api_admin_create_client,
    api_admin_update_client,
    api_admin_delete_client,
    api_admin_stats,
    api_admin_users,
    api_admin_create_user,
    api_admin_update_user,
    api_admin_delete_user,
    api_admin_reset_password,
    api_client_contacts,
    api_change_password,
)
from project835.drf_compat import admin_api, authenticated_api, public_api

# Keep the established URL and payload contract while dispatching API traffic
# through DRF.  Browser-rendered Django views above remain unchanged.
api_user_info = public_api(api_user_info)
api_login = public_api(api_login)
api_signup = public_api(api_signup)
api_totp_setup = authenticated_api(api_totp_setup)
api_totp_verify = authenticated_api(api_totp_verify)
api_logout = public_api(api_logout)
api_client_contacts = authenticated_api(api_client_contacts)
api_change_password = authenticated_api(api_change_password)

api_admin_clients = admin_api(api_admin_clients)
api_admin_create_client = admin_api(api_admin_create_client)
api_admin_update_client = admin_api(api_admin_update_client)
api_admin_delete_client = admin_api(api_admin_delete_client)
api_admin_stats = admin_api(api_admin_stats)
api_admin_users = admin_api(api_admin_users)
api_admin_create_user = admin_api(api_admin_create_user)
api_admin_update_user = admin_api(api_admin_update_user)
api_admin_delete_user = admin_api(api_admin_delete_user)
api_admin_reset_password = admin_api(api_admin_reset_password)

urlpatterns = [
    path("signup/", signup_view, name="signup"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("totp/setup/", totp_setup_view, name="totp_setup"),
    path("totp/verify/", totp_verify_view, name="totp_verify"),
    path("api/user/", api_user_info, name="api_user_info"),
    path("api/login/", api_login, name="api_login"),
    path("api/signup/", api_signup, name="api_signup"),
    path("api/totp/setup/", api_totp_setup, name="api_totp_setup"),
    path("api/totp/verify/", api_totp_verify, name="api_totp_verify"),
    path("api/logout/", api_logout, name="api_logout"),
    path("api/contacts/", api_client_contacts, name="api_client_contacts"),
    path("api/user/change-password/", api_change_password, name="api_change_password"),
    # Admin Panel API Endpoints
    path("api/admin/clients/", api_admin_clients, name="api_admin_clients"),
    path("api/admin/clients/create/", api_admin_create_client, name="api_admin_create_client"),
    path("api/admin/clients/<uuid:client_id>/update/", api_admin_update_client, name="api_admin_update_client"),
    path("api/admin/clients/<uuid:client_id>/delete/", api_admin_delete_client, name="api_admin_delete_client"),
    path("api/admin/stats/", api_admin_stats, name="api_admin_stats"),
    path("api/admin/users/", api_admin_users, name="api_admin_users"),
    path("api/admin/users/create/", api_admin_create_user, name="api_admin_create_user"),
    path("api/admin/users/<int:user_id>/update/", api_admin_update_user, name="api_admin_update_user"),
    path("api/admin/users/<int:user_id>/delete/", api_admin_delete_user, name="api_admin_delete_user"),
    path("api/admin/users/<int:user_id>/reset-password/", api_admin_reset_password, name="api_admin_reset_password"),
]
