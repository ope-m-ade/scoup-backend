import secrets

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.mail import send_mail
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from ..models import Faculty


User = get_user_model()


class EmailOrUsernameTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Accept either username or email in the default `username` field.
    """

    def validate(self, attrs):
        identifier = str(attrs.get("username", "")).strip()

        if "@" in identifier:
            user = User.objects.filter(email__iexact=identifier).first()
            if user:
                attrs["username"] = user.get_username()

        return super().validate(attrs)


class EmailOrUsernameTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailOrUsernameTokenObtainPairSerializer


@api_view(["POST"])
@permission_classes([AllowAny])
def forgot_password(request):
    """
    POST /api/auth/forgot-password/
    Body: { "email": "user@example.com" }

    Always returns 200 so we don't expose whether an email exists.
    Sends a reset link to the address if a matching faculty account exists.
    """
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"detail": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Rate limit: max 3 reset requests per email per 15 minutes
    rate_key = f"pwd_reset_rate_{email}"
    attempts = cache.get(rate_key, 0)
    if attempts >= 3:
        return Response({"detail": "Too many reset requests. Please wait 15 minutes before trying again."}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    cache.set(rate_key, attempts + 1, timeout=900)

    try:
        faculty = Faculty.objects.get(email__iexact=email)
        user = faculty.user
    except Faculty.DoesNotExist:
        # Silent — don't reveal whether the email exists
        return Response({"detail": "If that email is registered, a reset link has been sent."})
    except Exception:
        return Response({"detail": "If that email is registered, a reset link has been sent."})

    # Generate a secure token and store it in the cache for 1 hour
    token = secrets.token_urlsafe(32)
    cache_key = f"pwd_reset_{token}"
    cache.set(cache_key, user.pk, timeout=3600)

    # Build the reset URL — uses the frontend origin from the request
    origin = request.headers.get("Origin") or request.build_absolute_uri("/").rstrip("/")
    reset_url = f"{origin}/reset-password?token={token}"

    try:
        send_mail(
            subject="SCOUP — Password Reset Request",
            message=(
                f"Hello {faculty.first_name or user.username},\n\n"
                f"We received a request to reset your SCOUP password.\n\n"
                f"Click the link below to choose a new password (valid for 1 hour):\n"
                f"{reset_url}\n\n"
                f"If you did not request this, you can safely ignore this email.\n\n"
                f"— The SCOUP Team"
            ),
            from_email=None,  # uses DEFAULT_FROM_EMAIL from settings
            recipient_list=[email],
            fail_silently=True,
        )
    except Exception:
        pass  # Never expose mail errors to the client

    return Response({"detail": "If that email is registered, a reset link has been sent."})


@api_view(["POST"])
@permission_classes([AllowAny])
def reset_password(request):
    """
    POST /api/auth/reset-password/
    Body: { "token": "...", "password": "new_password" }
    """
    token    = (request.data.get("token") or "").strip()
    password = (request.data.get("password") or "").strip()

    if not token or not password:
        return Response(
            {"detail": "Token and new password are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 8:
        return Response(
            {"detail": "Password must be at least 8 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    cache_key = f"pwd_reset_{token}"
    user_pk = cache.get(cache_key)

    if not user_pk:
        return Response(
            {"detail": "This reset link is invalid or has expired."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        return Response(
            {"detail": "User not found."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user.set_password(password)
    user.save()
    cache.delete(cache_key)  # one-time use

    return Response({"detail": "Password updated successfully. You can now log in."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """
    POST /api/auth/change-password/
    Body: { "current_password": "...", "new_password": "..." }
    Authenticated faculty can change their own password.
    """
    current_password = (request.data.get("current_password") or "").strip()
    new_password     = (request.data.get("new_password") or "").strip()

    if not current_password or not new_password:
        return Response(
            {"detail": "Both current and new passwords are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(new_password) < 8:
        return Response(
            {"detail": "New password must be at least 8 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = request.user
    if not user.check_password(current_password):
        return Response(
            {"detail": "Current password is incorrect."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if current_password == new_password:
        return Response(
            {"detail": "New password must be different from the current password."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user.set_password(new_password)
    user.save()

    return Response({"detail": "Password changed successfully."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_institutional_otp(request):
    """
    POST /api/auth/send-otp/
    Body: { "institutional_email": "name@salisbury.edu" }
    Sends a 6-digit OTP to the given address. The faculty must be logged in.
    """
    institutional_email = (request.data.get("institutional_email") or "").strip().lower()
    if not institutional_email:
        return Response({"detail": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not (institutional_email.endswith("@salisbury.edu") or institutional_email.endswith("@gulls.salisbury.edu")):
        return Response({"detail": "Only Salisbury University email addresses are accepted (@salisbury.edu or @gulls.salisbury.edu)."}, status=status.HTTP_400_BAD_REQUEST)

    # Rate limit: max 3 OTP requests per email per 10 minutes
    otp_rate_key = f"otp_rate_{institutional_email}"
    otp_attempts = cache.get(otp_rate_key, 0)
    if otp_attempts >= 3:
        return Response({"detail": "Too many verification attempts. Please wait 10 minutes before trying again."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    try:
        faculty = request.user.faculty_profile
    except Exception:
        return Response({"detail": "Faculty profile not found."}, status=status.HTTP_404_NOT_FOUND)

    # Generate 6-digit OTP using a cryptographically secure source
    otp = f"{secrets.randbelow(1_000_000):06d}"
    cache_key = f"email_otp_{faculty.id}"
    cache.set(cache_key, {"otp": otp, "email": institutional_email}, timeout=600)

    try:
        send_mail(
            subject="SCOUP — Your Verification Code",
            message=(
                f"Hello,\n\n"
                f"Your SCOUP email verification code is:\n\n"
                f"    {otp}\n\n"
                f"This code expires in 10 minutes.\n\n"
                f"If you did not request this, please ignore this email.\n\n"
                f"— The SCOUP Team"
            ),
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[institutional_email],
            fail_silently=False,
        )
    except Exception as e:
        return Response({"detail": f"Failed to send email: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    cache.set(otp_rate_key, otp_attempts + 1, timeout=600)
    return Response({"detail": "Verification code sent."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_institutional_otp(request):
    """
    POST /api/auth/verify-otp/
    Body: { "otp": "123456" }
    Verifies the OTP. On success marks institutional_email_verified=True
    and notifies admin.
    """
    otp_input = (request.data.get("otp") or "").strip()
    if not otp_input:
        return Response({"detail": "Code is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        faculty = request.user.faculty_profile
    except Exception:
        return Response({"detail": "Faculty profile not found."}, status=status.HTTP_404_NOT_FOUND)

    cache_key = f"email_otp_{faculty.id}"
    cached = cache.get(cache_key)

    if not cached:
        return Response({"detail": "Code has expired. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)

    if cached["otp"] != otp_input:
        return Response({"detail": "Incorrect code. Please try again."}, status=status.HTTP_400_BAD_REQUEST)

    # Mark verified and auto-approve — verifying the institutional email is proof of faculty status
    faculty.institutional_email = cached["email"]
    faculty.institutional_email_verified = True
    faculty.is_approved = True
    faculty.save(update_fields=["institutional_email", "institutional_email_verified", "is_approved", "updated_at"])
    cache.delete(cache_key)

    # Notify admin
    admin_emails = list(
        User.objects.filter(is_staff=True).values_list("email", flat=True)
    )
    admin_emails = [e for e in admin_emails if e]
    if admin_emails:
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or request.user.username
        try:
            send_mail(
                subject="SCOUP — New Faculty Awaiting Approval",
                message=(
                    f"A faculty member has verified their institutional email and is awaiting profile approval.\n\n"
                    f"Name: {faculty_name}\n"
                    f"Institutional Email: {cached['email']}\n"
                    f"Login Email: {faculty.email or request.user.email or 'N/A'}\n\n"
                    f"Log in to the admin dashboard to review and approve their profile.\n\n"
                    f"— SCOUP System"
                ),
                from_email=None,
                recipient_list=admin_emails,
                fail_silently=True,
            )
        except Exception:
            pass  # Don't fail the request if admin notification fails

    return Response({"detail": "Email verified successfully. Your profile is now pending admin approval."})
