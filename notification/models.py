import datetime

try:
    import cPickle as pickle
except ImportError:
    import pickle

from django.db import models
from django.db.models.query import QuerySet
from django.conf import settings
from django.core.urlresolvers import reverse
from django.template import Context
from django.template.loader import render_to_string

from django.core.exceptions import ImproperlyConfigured

from django.contrib.sites.models import Site
from django.contrib.auth.models import User
from django.contrib.auth.models import AnonymousUser

from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic

from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ugettext, get_language, activate
try:
    from facebook import GraphAPI
    from decorators import daemonize
except ImportError:
    from notification.facebook import GraphAPI
    from notification.decorators import daemonize
# favour django-mailer but fall back to django.core.mail
if 'mailer' in settings.INSTALLED_APPS:
    from mailer import send_mail
else:
    from django.core.mail import send_mail


QUEUE_ALL = getattr(settings, "NOTIFICATION_QUEUE_ALL", False)
LAZY_RENDERING = getattr(settings, "LAZY_NOTIFICATION_RENDERING", False) #whether to store contexts or full rendered templates
FACEBOOK_ATTR = getattr(settings, "NOTIFICATION_FACEBOOK_ATTR", 'facebook_access_token')
PROFILES_ACTIVATED = getattr(settings, "AUTH_PROFILE_MODULE", False)


class LanguageStoreNotAvailable(Exception):
    pass

class NoticeType(models.Model):

    label = models.CharField(_('label'), max_length=40)
    display = models.CharField(_('display'), max_length=50)
    description = models.CharField(_('description'), max_length=100)

    # by default only on for media with sensitivity less than or equal to this number
    default = models.IntegerField(_('default'))

    def __unicode__(self):
        return self.label

    class Meta:
        verbose_name = _("notice type")
        verbose_name_plural = _("notice types")


# if this gets updated, the create() method below needs to be as well...
NOTICE_MEDIA = (
    ("1", _("Email")),
    ("2", _("Facebook")),
)

# how spam-sensitive is the medium
NOTICE_MEDIA_DEFAULTS = {
    "1": 2, # email
    "2": 3,# facebook
}

class NoticeSetting(models.Model):
    """
    Indicates, for a given user, whether to send notifications
    of a given type to a given medium.
    """

    user = models.ForeignKey(User, verbose_name=_('user'))
    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))
    medium = models.CharField(_('medium'), max_length=1, choices=NOTICE_MEDIA)
    send = models.BooleanField(_('send'))

    class Meta:
        verbose_name = _("notice setting")
        verbose_name_plural = _("notice settings")
        unique_together = ("user", "notice_type", "medium")

def get_notification_setting(user, notice_type, medium):
    try:
        return NoticeSetting.objects.get(user=user, notice_type=notice_type, medium=medium)
    except NoticeSetting.DoesNotExist:
        default = (NOTICE_MEDIA_DEFAULTS[medium] <= notice_type.default)
        setting = NoticeSetting(user=user, notice_type=notice_type, medium=medium, send=default)
        setting.save()
        return setting

def should_send(user, notice_type, medium):
    return get_notification_setting(user, notice_type, medium).send


class NoticeManager(models.Manager):

    def notices_for(self, user, archived=False, unseen=None, on_site=None, context = None):
        """
        returns Notice objects for the given user.

        If archived=False, it only include notices not archived.
        If archived=True, it returns all notices for that user.

        If unseen=None, it includes all notices.
        If unseen=True, return only unseen notices.
        If unseen=False, return only seen notices.
        If context != None, return only notices for the given context
        """
        if archived:
            qs = self.filter(user=user)
        else:
            qs = self.filter(user=user, archived=archived)
        if unseen is not None:
            qs = qs.filter(unseen=unseen)
        if on_site is not None:
            qs = qs.filter(on_site=on_site)
        if context:
            qs = qs.filter(context__content_type=ContentType.objects.get_for_model(context),
                                                context__object_id=context.pk)
        return qs

    def unseen_count_for(self, user, **kwargs):
        """
        returns the number of unseen notices for the given user but does not
        mark them seen
        """
        return self.notices_for(user, unseen=True, **kwargs).count()

class ActivityContext(models.Model):
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey('content_type', 'object_id')
    
class Notice(models.Model):

    user = models.ForeignKey(User, verbose_name=_('user'))
    message = models.TextField(_('message'))
    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))
    added = models.DateTimeField(_('added'), default=datetime.datetime.now)
    unseen = models.BooleanField(_('unseen'), default=True)
    archived = models.BooleanField(_('archived'), default=False)
    on_site = models.BooleanField(_('on site'))
    context = models.ForeignKey(ActivityContext, null=True)

    objects = NoticeManager()

    def __unicode__(self):
        return self.as_html()
    
    def as_html(self):
        """Try to render this notice as an html string"""
        #don't believe in settings...
        try:
            template_context = pickle.loads(self.message.decode("base64"))
            return get_formatted_messages(('notice.html',), self.notice_type.label, template_context)['notice.html']
        except:
            return self.message
        

    def archive(self):
        self.archived = True
        self.save()

    def is_unseen(self):
        """
        returns value of self.unseen but also changes it to false.

        Use this in a template to mark an unseen notice differently the first
        time it is shown.
        """
        unseen = self.unseen
        if unseen:
            self.unseen = False
            self.save()
        return unseen

    class Meta:
        ordering = ["-added"]
        verbose_name = _("notice")
        verbose_name_plural = _("notices")

    def get_absolute_url(self):
        return ("notification_notice", [str(self.pk)])
    get_absolute_url = models.permalink(get_absolute_url)


class NoticeQueueBatch(models.Model):
    """
    A queued notice.
    Denormalized data for a notice.
    """
    pickled_data = models.TextField()

def create_notice_type(label, display, description, default=2, verbosity=1):
    """
    Creates a new NoticeType.

    This is intended to be used by other apps as a post_syncdb manangement step.
    """
    try:
        notice_type = NoticeType.objects.get(label=label)
        updated = False
        if display != notice_type.display:
            notice_type.display = display
            updated = True
        if description != notice_type.description:
            notice_type.description = description
            updated = True
        if default != notice_type.default:
            notice_type.default = default
            updated = True
        if updated:
            notice_type.save()
            if verbosity > 1:
                print "Updated %s NoticeType" % label
    except NoticeType.DoesNotExist:
        NoticeType(label=label, display=display, description=description, default=default).save()
        if verbosity > 1:
            print "Created %s NoticeType" % label

def get_notification_language(user):
    """
    Returns site-specific notification language for this user. Raises
    LanguageStoreNotAvailable if this site does not use translated
    notifications.
    """
    if getattr(settings, 'NOTIFICATION_LANGUAGE_MODULE', False):
        try:
            app_label, model_name = settings.NOTIFICATION_LANGUAGE_MODULE.split('.')
            model = models.get_model(app_label, model_name)
            language_model = model._default_manager.get(user__id__exact=user.id)
            if hasattr(language_model, 'language'):
                return language_model.language
        except (ImportError, ImproperlyConfigured, model.DoesNotExist):
            raise LanguageStoreNotAvailable
    raise LanguageStoreNotAvailable

def get_formatted_messages(formats, label, context):
    """
    Returns a dictionary with the format identifier as the key. The values are
    are fully rendered templates with the given context.
    """
    format_templates = {}
    for format in formats:
        # conditionally turn off autoescaping for .txt extensions in format
        if format.endswith(".txt"):
            context.autoescape = False
        else:
            context.autoescape = True
        format_templates[format] = render_to_string((
            'notification/%s/%s' % (label, format),
            'notification/%s' % format), context_instance=context)
    return format_templates

def send_now(users, label, extra_context=None, on_site=True, context=None):
    """
    Creates a new notice.

    This is intended to be how other apps create new notices.

    notification.send(user, 'friends_invite_sent', {
        'spam': 'eggs',
        'foo': 'bar',
    )
    
    You can pass in on_site=False to prevent the notice emitted from being
    displayed on the site.
    """
    if extra_context is None:
        extra_context = {}
    
    #create the ActivityContext object
    if context:
        ct = ContentType.objects.get_for_model(context)
        m = ct.model
        context = ActivityContext.objects.get_or_create(content_type = ct,
                                                        object_id=context.pk)[0]
    notice_type = NoticeType.objects.get(label=label)

    current_site = Site.objects.get_current()
    
    notices_url = u"http://%s%s" % (
                    unicode(current_site),
                    reverse("notification_notice_settings"))
    
        
    current_language = get_language()

    formats = (
        'short.txt',
        'full.txt',        
        'full.html',
    ) # TODO make formats configurable
    if not LAZY_RENDERING and on_site:
        formats += ('notice.html',) 

    for user in users:
        recipients = []
        # get user language for user from language store defined in
        # NOTIFICATION_LANGUAGE_MODULE setting
        try:
            language = get_notification_language(user)
        except LanguageStoreNotAvailable:
            language = None

        if language is not None:
            # activate the user's language
            activate(language)

        # update template context with user specific translations
        template_context = Context({
            "user": user,
            "notice": ugettext(notice_type.display),
            "notices_url": notices_url,
            "current_site": current_site,
        })
        template_context.update(extra_context)

        # get prerendered format messages
        messages = get_formatted_messages(formats, label, template_context)

        # Strip newlines from subject
        subject = ''.join(render_to_string('notification/email_subject.txt', {
            'message': messages['short.txt'],
        }, template_context).splitlines())

        body = render_to_string('notification/email_body.txt', {
            'message': messages['full.txt'],
        }, template_context)
        if LAZY_RENDERING and on_site:
            #re-create the context to avoid including the other rendered templates
            ctx =Context({
            "user": user,
            "notice": ugettext(notice_type.display),
            "notices_url": notices_url,
            "current_site": current_site,
            })
            ctx.update(extra_context)
            notice = Notice.objects.create(user=user, message=pickle.dumps(ctx).encode("base64"),
            notice_type=notice_type, on_site=on_site, context = context)
        else:
            notice = Notice.objects.create(user=user, message=messages['notice.html'],
            notice_type=notice_type, on_site=on_site, context = context)
        
        if should_send(user, notice_type, "1") and user.email: # Email
            recipients.append(user.email)
        
        #facebook
        if should_send(user, notice_type, "2") and PROFILES_ACTIVATED:
            #try to guess the facebook stuff:
            if not 'name' in extra_context:
                extra_context['name'] = current_site.domain
            if not 'description' in extra_context:
                extra_context['description']= subject
            if not 'link' in extra_context:
                extra_context['link'] = notices_url
            if not 'picture' in extra_context and hasattr(settings, 'NOTIFICATION_SITE_PICTURE'):
                extra_context['picture'] = settings.NOTIFICATION_SITE_PICTURE
            send_to_facebook(user, extra_context)
            
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)

    # reset environment to original language
    activate(current_language)

@daemonize
def send_to_facebook(user, context={}):
    """Send a wall post to a user's facebook profile
    
      It's run as a daemon, to avoid delaying too much the response time of the original response
    """
    
    #only leave the facebook attrs:
    facebook_attrs = ['name', 'link', 'caption', 'description', 'picture']
    for e in context.keys():
        if e not in facebook_attrs:
            del context[e]  
    
    #description="", picture="", link="http://escolarea.com", message="", caption="" 
    if "http://" not in context.get('link', ''):
        #build the full url        
        context['link'] = u"http://%s%s" % (
                    unicode(Site.objects.get_current()), context.get('link', ''))
        
    access_token = getattr(user.get_profile(), FACEBOOK_ATTR, None)
    if context and access_token:
        graph_api = GraphAPI(access_token)
        graph_api.put_wall_post(message=context.get('message', ''),
                                attachment=context)


def send(*args, **kwargs):
    """
    A basic interface around both queue and send_now. This honors a global
    flag NOTIFICATION_QUEUE_ALL that helps determine whether all calls should
    be queued or not. A per call ``queue`` or ``now`` keyword argument can be
    used to always override the default global behavior.
    """
    queue_flag = kwargs.pop("queue", False)
    now_flag = kwargs.pop("now", False)
    assert not (queue_flag and now_flag), "'queue' and 'now' cannot both be True."
    if queue_flag:
        return queue(*args, **kwargs)
    elif now_flag:
        return send_now(*args, **kwargs)
    else:
        if QUEUE_ALL:
            return queue(*args, **kwargs)
        else:
            return send_now(*args, **kwargs)
        
def queue(users, label, extra_context=None, on_site=True, context = None):
    """
    Queue the notification in NoticeQueueBatch. This allows for large amounts
    of user notifications to be deferred to a seperate process running outside
    the webserver.
    """
    if extra_context is None:
        extra_context = {}
    if isinstance(users, QuerySet):
        users = [row["pk"] for row in users.values("pk")]
    else:
        users = [user.pk for user in users]
    notices = []
    for user in users:
        notices.append((user, label, extra_context, on_site, context))
    NoticeQueueBatch(pickled_data=pickle.dumps(notices).encode("base64")).save()

class ObservedItemManager(models.Manager):

    def all_for(self, observed, signal):
        """
        Returns all ObservedItems for an observed object,
        to be sent when a signal is emited.
        """
        content_type = ContentType.objects.get_for_model(observed)
        observed_items = self.filter(content_type=content_type, object_id=observed.id, signal=signal)
        return observed_items

    def get_for(self, observed, observer, signal):
        content_type = ContentType.objects.get_for_model(observed)
        observed_item = self.get(content_type=content_type, object_id=observed.id, user=observer, signal=signal)
        return observed_item


class ObservedItem(models.Model):

    user = models.ForeignKey(User, verbose_name=_('user'))

    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    observed_object = generic.GenericForeignKey('content_type', 'object_id')

    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))

    added = models.DateTimeField(_('added'), default=datetime.datetime.now)

    # the signal that will be listened to send the notice
    signal = models.TextField(verbose_name=_('signal'))

    objects = ObservedItemManager()

    class Meta:
        ordering = ['-added']
        verbose_name = _('observed item')
        verbose_name_plural = _('observed items')

    def send_notice(self):
        send([self.user], self.notice_type.label,
             {'observed': self.observed_object})


def observe(observed, observer, notice_type_label, signal='post_save'):
    """
    Create a new ObservedItem.

    To be used by applications to register a user as an observer for some object.
    """
    notice_type = NoticeType.objects.get(label=notice_type_label)
    observed_item = ObservedItem(user=observer, observed_object=observed,
                                 notice_type=notice_type, signal=signal)
    observed_item.save()
    return observed_item

def stop_observing(observed, observer, signal='post_save'):
    """
    Remove an observed item.
    """
    observed_item = ObservedItem.objects.get_for(observed, observer, signal)
    observed_item.delete()

def send_observation_notices_for(observed, signal='post_save'):
    """
    Send a notice for each registered user about an observed object.
    """
    observed_items = ObservedItem.objects.all_for(observed, signal)
    for observed_item in observed_items:
        observed_item.send_notice()
    return observed_items

def is_observing(observed, observer, signal='post_save'):
    if isinstance(observer, AnonymousUser):
        return False
    try:
        observed_items = ObservedItem.objects.get_for(observed, observer, signal)
        return True
    except ObservedItem.DoesNotExist:
        return False
    except ObservedItem.MultipleObjectsReturned:
        return True

def handle_observations(sender, instance, *args, **kw):
    send_observation_notices_for(instance)


#alternative for observations
from django.db.models.signals import post_save

for model_path, callback_path in settings.AUTO_NOTIFY:
    #get model module
    msplt = model_path.split('.')
    model_module_name, model_class_name = '.'.join(msplt[:-1]), msplt[-1]
    model_module = __import__(model_module_name, globals(), locals(), [model_class_name])
    model_class = getattr(model_module, model_class_name)
    
    csplt = callback_path.split('.')
    callback_module_name, callback_name = '.'.join(csplt[:-1]), csplt[-1]
    callback_module = __import__(callback_module_name, globals(), locals(), [callback_name])
    callback = getattr(callback_module, callback_name)
    
    post_save.connect(callback, sender=model_class)    
