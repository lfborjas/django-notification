from django.core.urlresolvers import reverse
from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponseRedirect, Http404
from django.template import RequestContext
from django.contrib.auth.decorators import login_required
from django.contrib.syndication.views import feed

from notification.models import *
from notification.decorators import basic_auth_required, simple_basic_auth_callback
from notification.feeds import NoticeUserFeed, ContextNoticeFeed
try:
    import json
except ImportError:
    import django.utils.simplejson as json
    
from django.conf import settings
from django.http import HttpResponse

@basic_auth_required(realm='Notices Feed', callback_func=simple_basic_auth_callback)
def feed_for_user(request):
    url = "feed/%s" % request.user.username
    return feed(request, url, {
        "feed": NoticeUserFeed,
    })
    
@basic_auth_required(realm='Notices Feed', callback_func=simple_basic_auth_callback)
def json_feed_for_user(request):
    return HttpResponse(json.dumps({'notifications': [unicode(e) for e in Notice.objects.notices_for(request.user, on_site=True)]},
                                    ensure_ascii=False),
                        mimetype="application/json")

@basic_auth_required(realm='Context Notices Feed', callback_func=simple_basic_auth_callback)
def context_feed_for_user(request, context, object_id):
    url = "%s/%s/feed/%s" % (context, object_id, request.user.username)
    return feed(request, url, {
        "feed": ContextNoticeFeed,
    })

@basic_auth_required(realm='Context Notices Feed', callback_func=simple_basic_auth_callback)
def context_json_feed_for_user(request, context, object_id):
    if context not in settings.NOTIFICATION_CONTEXTS.keys():
        return HttpResponse(json.dumps({'notifications': []}), mimetype="application/json")
        
    app, model = settings.NOTIFICATION_CONTEXTS[context].split('.')    
    try:
        context_object = ActivityContext.objects.get(content_type__app_label=app, content_type__model=model, object_id = object_id)
        notices = Notice.objects.notices_for(request.user, on_site=True,
                                         context = context_object.content_object)        
    except ActivityContext.DoesNotExist:
        notices = []
    
    return HttpResponse(json.dumps({'notifications': [unicode(e) for e in notices]}, ensure_ascii=False), mimetype="application/json")
    
@login_required
def context_notices(request, context, object_id):    
    """Get notifications for a given context"""    
    if context not in settings.NOTIFICATION_CONTEXTS.keys():
        raise Http404    
    app, model = settings.NOTIFICATION_CONTEXTS[context].split('.')
    context_object = None
    try:
        context_object = ActivityContext.objects.get(content_type__app_label=app, content_type__model=model, object_id = object_id)
        notices = Notice.objects.notices_for(request.user, on_site=True,
                                         context = context_object.content_object)        
    except ActivityContext.DoesNotExist:
        notices = []
        
    notice_types = NoticeType.objects.all()    
    return render_to_response("notification/context/%s.html" % context, {
        "notices": notices,
        "notice_types": notice_types, #to filter!  
        "object": context_object.content_object if hasattr(context_object, "content_object") else None
    }, context_instance=RequestContext(request))
    
@login_required
def notices(request):
    """This appears to show all notices and also the settings..."""
    
    notice_types = NoticeType.objects.all()
    notices = Notice.objects.notices_for(request.user, on_site=True)
    settings_table = []
    for notice_type in NoticeType.objects.all():
        settings_row = []
        for medium_id, medium_display in NOTICE_MEDIA:
            form_label = "%s_%s" % (notice_type.label, medium_id)
            setting = get_notification_setting(request.user, notice_type, medium_id)
            if request.method == "POST":
                if request.POST.get(form_label) == "on":
                    setting.send = True
                else:
                    setting.send = False
                setting.save()
            settings_row.append((form_label, setting.send))
        settings_table.append({"notice_type": notice_type, "cells": settings_row})
    
    notice_settings = {
        "column_headers": [medium_display for medium_id, medium_display in NOTICE_MEDIA],
        "rows": settings_table,
    }
    
    return render_to_response("notification/notices.html", {
        "notices": notices,
        "notice_types": notice_types,
        "notice_settings": notice_settings,
    }, context_instance=RequestContext(request))

@login_required
def single(request, id):
    notice = get_object_or_404(Notice, id=id)
    if request.user == notice.user:
        return render_to_response("notification/single.html", {
            "notice": notice,
        }, context_instance=RequestContext(request))
    raise Http404

#TODO: ajaxify all this stuff
@login_required
def archive(request, noticeid=None, next_page=None):
    if noticeid:
        try:
            notice = Notice.objects.get(id=noticeid)
            if request.user == notice.user or request.user.is_superuser:
                notice.archive()
            else:   # you can archive other users' notices
                    # only if you are superuser.
                return HttpResponseRedirect(next_page)
        except Notice.DoesNotExist:
            return HttpResponseRedirect(next_page)
    return HttpResponseRedirect(next_page)

@login_required
def delete(request, noticeid=None, next_page=None):
    if noticeid:
        try:
            notice = Notice.objects.get(id=noticeid)
            if request.user == notice.user or request.user.is_superuser:
                notice.delete()
            else:   # you can delete other users' notices
                    # only if you are superuser.
                return HttpResponseRedirect(next_page)
        except Notice.DoesNotExist:
            return HttpResponseRedirect(next_page)
    return HttpResponseRedirect(next_page)

@login_required
def mark_all_seen(request):
    for notice in Notice.objects.notices_for(request.user, unseen=True):
        notice.unseen = False
        notice.save()
    return HttpResponseRedirect(reverse("notification_notices"))
    