from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from ..models import ContactTeamMember, ContactPageSettings
from ..serializers import ContactTeamMemberSerializer, ContactPageSettingsSerializer


@api_view(["GET"])
@permission_classes([AllowAny])
def contact_team_list(request):
    members = ContactTeamMember.objects.filter(is_visible=True)
    serializer = ContactTeamMemberSerializer(members, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def admin_contact_team_list(request):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    if request.method == "GET":
        members = ContactTeamMember.objects.all()
        serializer = ContactTeamMemberSerializer(members, many=True, context={"request": request})
        return Response(serializer.data)
    serializer = ContactTeamMemberSerializer(data=request.data, context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data, status=201)
    return Response(serializer.errors, status=400)


@api_view(["PUT", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def admin_contact_team_detail(request, pk):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    try:
        member = ContactTeamMember.objects.get(pk=pk)
    except ContactTeamMember.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        member.delete()
        return Response(status=204)
    serializer = ContactTeamMemberSerializer(member, data=request.data, partial=request.method == "PATCH", context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)


@api_view(["GET"])
@permission_classes([AllowAny])
def contact_settings(request):
    obj, _ = ContactPageSettings.objects.get_or_create(pk=1)
    serializer = ContactPageSettingsSerializer(obj)
    return Response(serializer.data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_contact_settings(request):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    obj, _ = ContactPageSettings.objects.get_or_create(pk=1)
    serializer = ContactPageSettingsSerializer(obj, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_contact_team_photo_upload(request, pk):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    try:
        member = ContactTeamMember.objects.get(pk=pk)
    except ContactTeamMember.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    if "photo" not in request.FILES:
        return Response({"detail": "No photo provided."}, status=400)
    member.photo = request.FILES["photo"]
    member.save()
    serializer = ContactTeamMemberSerializer(member, context={"request": request})
    return Response(serializer.data)
