import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.sites.shortcuts import get_current_site
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse_lazy
from django.db.models import Case
from django.db.models import IntegerField
from django.db.models import OuterRef
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import Value
from django.db.models import When
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.http import HttpResponseNotFound
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import TemplateView
from enum import Enum
from le_utils.constants import content_kinds
from rest_framework.authentication import BasicAuthentication
from rest_framework.authentication import SessionAuthentication
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from contentcuration.api import activate_channel
from contentcuration.api import add_editor_to_channel
from contentcuration.api import get_staged_diff
from contentcuration.decorators import browser_is_supported
from contentcuration.decorators import can_access_channel
from contentcuration.decorators import can_edit_channel
from contentcuration.decorators import has_accepted_policies
from contentcuration.models import Channel
from contentcuration.models import ContentNode
from contentcuration.models import Invitation
from contentcuration.models import SecretToken
from contentcuration.models import User
from contentcuration.models import VIEW_ACCESS
from contentcuration.serializers import AltChannelListSerializer
from contentcuration.serializers import ChannelSetSerializer
from contentcuration.serializers import ContentNodeSerializer
from contentcuration.serializers import CurrentUserSerializer
from contentcuration.serializers import RootNodeSerializer
from contentcuration.serializers import SimplifiedChannelProbeCheckSerializer
from contentcuration.serializers import TaskSerializer
from contentcuration.serializers import UserChannelListSerializer
from contentcuration.tasks import create_async_task
from contentcuration.tasks import generatechannelcsv_task
from contentcuration.utils.messages import get_messages

PUBLIC_CHANNELS_CACHE_DURATION = 30  # seconds


@browser_is_supported
def base(request):
    if request.user.is_authenticated():
        return redirect('channels')
    else:
        return redirect('accounts/login')


""" HEALTH CHECKS """


def health(request):
    c = Channel.objects.first()
    if c:
        return HttpResponse(c.name)
    else:
        return HttpResponse("No channels created yet!")


def stealth(request):
    return HttpResponse("<3")


@api_view(['GET'])
@authentication_classes((TokenAuthentication, SessionAuthentication))
@permission_classes((IsAuthenticated,))
def get_prober_channel(request):
    if not request.user.is_admin:
        return HttpResponseForbidden()

    channel = Channel.objects.filter(editors=request.user).first()
    if not channel:
        channel = Channel.objects.create(name="Prober channel", editors=[request.user])

    return Response(SimplifiedChannelProbeCheckSerializer(channel).data)


""" END HEALTH CHECKS """


def get_or_set_cached_constants(constant, serializer):
    cached_data = cache.get(constant.__name__)
    if cached_data:
        return cached_data
    constant_objects = constant.objects.all()
    constant_serializer = serializer(constant_objects, many=True)
    constant_data = JSONRenderer().render(constant_serializer.data)
    cache.set(constant.__name__, constant_data, None)
    return constant_data


def redirect_to_channel_edit(request, channel_id):
    return redirect(reverse_lazy('channel', kwargs={'channel_id': channel_id}))


def redirect_to_channel_view(request, channel_id):
    return redirect(reverse_lazy('channel_view_only', kwargs={'channel_id': channel_id}))


def channel_page(request, channel, allow_edit=False, staging=False):
    channel_serializer = ChannelSerializer(channel)
    channel_list = Channel.objects.select_related('main_tree').prefetch_related('editors').prefetch_related('viewers')\
                          .exclude(id=channel.pk).filter(Q(deleted=False) & (Q(editors=request.user) | Q(viewers=request.user)))\
                          .annotate(is_view_only=Case(When(editors=request.user, then=Value(0)), default=Value(1), output_field=IntegerField()))\
                          .distinct().values("id", "name", "is_view_only").order_by('name')

    token = None
    if channel.secret_tokens.filter(is_primary=True).exists():
        token = channel.secret_tokens.filter(is_primary=True).first().token
        token = token[:5] + "-" + token[5:]

    json_renderer = JSONRenderer()
    return render(request, 'channel_edit.html', {"allow_edit": allow_edit,
                                                 "staging": staging,
                                                 "is_public": channel.public,
                                                 "channel": json_renderer.render(channel_serializer.data),
                                                 "channel_id": channel.pk,
                                                 "channel_name": channel.name,
                                                 "ricecooker_version": channel.ricecooker_version,
                                                 "channel_list": channel_list,
                                                 "current_user": json_renderer.render(CurrentUserSerializer(request.user).data),
                                                 "preferences": json.dumps(channel.content_defaults),
                                                 "messages": get_messages(),
                                                 "primary_token": token or channel.pk,
                                                 "title": settings.DEFAULT_TITLE,
                                                 })


@login_required
@browser_is_supported
@has_accepted_policies
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel_list(request):
    return render(request, 'channel_list.html', {"channel_name": False,
                                                 "current_user": JSONRenderer().render(UserChannelListSerializer(request.user).data),
                                                 "user_preferences": json.dumps(request.user.content_defaults),
                                                 "messages": get_messages(),
                                                 })


@api_view(['GET'])
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def get_user_channel_sets(request):
    sets = request.user.channel_sets.prefetch_related('secret_token__channels', 'editors').select_related('secret_token')
    channelset_serializer = ChannelSetSerializer(sets, many=True)
    return Response(channelset_serializer.data)


@login_required
@browser_is_supported
@has_accepted_policies
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel(request, channel_id):
    channel = get_object_or_404(Channel, id=channel_id, deleted=False)

    # Check user has permission to view channel
    if not channel.editors.filter(id=request.user.id).exists() and not request.user.is_admin:
        return redirect(reverse_lazy('channel_view_only', kwargs={'channel_id': channel_id}))

    return channel_page(request, channel, allow_edit=True)


@login_required
@browser_is_supported
@can_access_channel
@has_accepted_policies
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel_view_only(request, channel_id):
    channel = get_object_or_404(Channel, id=channel_id, deleted=False)
    return channel_page(request, channel)


@login_required
@browser_is_supported
@can_edit_channel
@has_accepted_policies
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel_staging(request, channel_id):
    channel = Channel.objects.get(pk=channel_id)

    if not channel.staging_tree:
        return render(request, 'staging_not_found.html')

    return channel_page(request, channel, allow_edit=True, staging=True)


@csrf_exempt
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def publish_channel(request):
    logging.debug("Entering the publish_channel endpoint")
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")

    data = json.loads(request.body)

    try:
        channel_id = data["channel_id"]
        request.user.can_edit(channel_id)

        task_info = {
            'user': request.user,
            'metadata': {
                'affects': {
                    'channels': [channel_id]
                }}
        }

        task_args = {
            'user_id': request.user.pk,
            'channel_id': channel_id,
        }

        task, task_info = create_async_task('export-channel', task_info, task_args)
        return HttpResponse(JSONRenderer().render(TaskSerializer(task_info).data))
    except KeyError:
        raise ObjectDoesNotExist("Missing attribute from data: {}".format(data))


class SQCount(Subquery):
    # Include ALIAS at the end to support Postgres
    template = "(SELECT COUNT(%(field)s) FROM (%(subquery)s) AS %(field)s__sum)"
    output_field = IntegerField()


def map_channel_data(channel):
    channel["id"] = channel.pop("main_tree__id")
    channel["title"] = channel.pop("name")
    if len(channel["children"]) == 1 and channel["children"][0] is None:
        channel["children"] = []
    channel["metadata"] = {
        "resource_count": channel.pop("resource_count")
    }
    return channel


@api_view(['GET'])
@authentication_classes((TokenAuthentication, SessionAuthentication))
@permission_classes((IsAuthenticated,))
def accessible_channels(request, channel_id):
    # Used for import modal
    # Returns a list of objects with the following parameters:
    # id, title, resource_count, children
    channels = Channel.objects.filter(Q(deleted=False) & (Q(public=True) | Q(editors=request.user) | Q(viewers=request.user)))\
                              .exclude(pk=channel_id).select_related("main_tree")
    channel_main_tree_nodes = ContentNode.objects.filter(
        tree_id=OuterRef("main_tree__tree_id")
    )
    # Add the unique count of distinct non-topic node content_ids
    non_topic_content_ids = (
        channel_main_tree_nodes.exclude(kind_id=content_kinds.TOPIC)
        .order_by("content_id")
        .distinct("content_id")
        .values_list("content_id", flat=True)
    )
    channels = channels.annotate(
        resource_count=SQCount(non_topic_content_ids, field="content_id"),
        children=ArrayAgg("main_tree__children"),
    )
    channels_data = channels.values("name", "resource_count", "children", "main_tree__id")

    return Response(map(map_channel_data, channels_data))


@api_view(['POST'])
def accept_channel_invite(request):
    invitation = Invitation.objects.get(pk=request.data.get('invitation_id'))
    channel = invitation.channel
    channel.is_view_only = invitation.share_mode == VIEW_ACCESS
    channel_serializer = AltChannelListSerializer(channel)
    add_editor_to_channel(invitation)

    return Response(channel_serializer.data)


def activate_channel_endpoint(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")

    data = json.loads(request.body)
    channel = Channel.objects.get(pk=data['channel_id'])
    try:
        activate_channel(channel, request.user)
    except PermissionDenied as e:
        return HttpResponseForbidden(str(e))

    return HttpResponse(json.dumps({"success": True}))


def get_staged_diff_endpoint(request):
    if request.method == 'POST':
        return HttpResponse(json.dumps(get_staged_diff(json.loads(request.body)['channel_id'])))

    return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")


@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def add_bookmark(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")

    data = json.loads(request.body)

    try:
        user = User.objects.get(pk=data["user_id"])
        channel = Channel.objects.get(pk=data["channel_id"])
        channel.bookmarked_by.add(user)
        channel.save()

        return HttpResponse(json.dumps({"success": True}))
    except ObjectDoesNotExist:
        return HttpResponseNotFound('Channel with id {} not found'.format(data["channel_id"]))


@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def remove_bookmark(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")

    data = json.loads(request.body)

    try:
        user = User.objects.get(pk=data["user_id"])
        channel = Channel.objects.get(pk=data["channel_id"])
        channel.bookmarked_by.remove(user)
        channel.save()

        return HttpResponse(json.dumps({"success": True}))
    except ObjectDoesNotExist:
        return HttpResponseNotFound('Channel with id {} not found'.format(data["channel_id"]))


@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def set_channel_priority(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")

    data = json.loads(request.body)

    try:
        channel = Channel.objects.get(pk=data["channel_id"])
        channel.priority = data["priority"]
        channel.save()

        return HttpResponse(json.dumps({"success": True}))
    except ObjectDoesNotExist:
        return HttpResponseNotFound('Channel with id {} not found'.format(data["channel_id"]))


@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def download_channel_content_csv(request, channel_id):
    """ Writes list of channels to csv, which is then emailed """
    site = get_current_site(request)
    generatechannelcsv_task.delay(channel_id, site.domain, request.user.id)

    return HttpResponse({"success": True})


@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def save_token_to_channels(request, token):
    channel_ids = json.loads(request.body)
    channels = Channel.objects.filter(pk__in=channel_ids)
    token = SecretToken.objects.get(token=token)
    token.set_channels(channels)

    return HttpResponse({"success": True})


class SandboxView(TemplateView):
    template_name = "sandbox.html"

    def get_context_data(self, **kwargs):
        kwargs = super(SandboxView, self).get_context_data(**kwargs)

        active_channels = Channel.objects.filter(Q(editors=self.request.user) | Q(public=True))
        active_tree_ids = active_channels.values_list('main_tree__tree_id', flat=True)
        active_nodes = ContentNode.objects.filter(tree_id__in=active_tree_ids)
        nodes = []

        # Get a node of every kind
        for kind, _ in content_kinds.choices:
            node = active_nodes.filter(kind_id=kind, freeze_authoring_data=False).first()
            if node:
                nodes.append(ContentNodeSerializer(node).data)

        # Add an imported node
        imported_node = active_nodes.filter(freeze_authoring_data=True).exclude(kind_id=content_kinds.TOPIC).first()
        if imported_node:
            nodes.append(ContentNodeSerializer(imported_node).data)

        kwargs.update({"nodes": JSONRenderer().render(nodes),
                       "channel": active_channels.first().pk,
                       "current_user": JSONRenderer().render(CurrentUserSerializer(self.request.user).data)
                       })
        return kwargs
