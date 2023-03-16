import math
from datetime import timedelta

import croniter
from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.contenttypes.fields import GenericRelation
from django.core.exceptions import ValidationError
from django.db import models
from django.templatetags.tz import utc
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_rq.queues import get_queue
from model_utils import Choices
from model_utils.models import TimeStampedModel

from scheduler import tools, logger
from scheduler.models.args import JobArg, JobKwarg

RQ_SCHEDULER_INTERVAL = getattr(settings, "DJANGO_RQ_SCHEDULER_INTERVAL", 60)


def callback_save_job(job, connection, result, *args, **kwargs):
    model_name = job.meta.get('job_type', None)
    if model_name is None:
        return
    model = apps.get_model(app_label='scheduler', model_name=model_name)
    scheduled_job = model.objects.filter(job_id=job.id).first()
    if scheduled_job:
        scheduled_job.unschedule()
        scheduled_job.schedule()
        scheduled_job.save()


class BaseJob(TimeStampedModel):
    JOB_TYPE = 'BaseJob'
    name = models.CharField(_('name'), max_length=128, unique=True)
    callable = models.CharField(_('callable'), max_length=2048)
    callable_args = GenericRelation(JobArg, related_query_name='args')
    callable_kwargs = GenericRelation(JobKwarg, related_query_name='kwargs')
    enabled = models.BooleanField(_('enabled'), default=True)
    queue = models.CharField(
        _('queue'),
        max_length=16,
        help_text='Queue name', )
    job_id = models.CharField(
        _('job id'), max_length=128, editable=False, blank=True, null=True)
    repeat = models.PositiveIntegerField(_('repeat'), blank=True, null=True)
    at_front = models.BooleanField(_('At front'), default=False, blank=True, null=True)
    timeout = models.IntegerField(
        _('timeout'), blank=True, null=True,
        help_text=_(
            'Timeout specifies the maximum runtime, in seconds, for the job '
            'before it\'ll be considered \'lost\'. Blank uses the default '
            'timeout.'
        )
    )
    result_ttl = models.IntegerField(
        _('result ttl'), blank=True, null=True,
        help_text=_('The TTL value (in seconds) of the job result. -1: '
                    'Result never expires, you should delete jobs manually. '
                    '0: Result gets deleted immediately. >0: Result expires '
                    'after n seconds.')
    )

    def __str__(self):
        return f'{self.JOB_TYPE}[{self.name}={self.callable}()]'

    def callable_func(self):
        return tools.callable_func(self.callable)

    def clean(self):
        self.clean_callable()
        self.clean_queue()

    def clean_callable(self):
        try:
            tools.callable_func(self.callable)
        except Exception:
            raise ValidationError({
                'callable': ValidationError(
                    _('Invalid callable, must be importable'), code='invalid')
            })

    def clean_queue(self):
        queue_keys = settings.RQ_QUEUES.keys()
        if self.queue not in queue_keys:
            raise ValidationError({
                'queue': ValidationError(
                    _('Invalid queue, must be one of: {}'.format(
                        ', '.join(queue_keys))), code='invalid')
            })

    @admin.display(boolean=True, description=_('is scheduled?'))
    def is_scheduled(self) -> bool:
        if not self.job_id:
            return False
        scheduled_jobs = self._get_rqueue().scheduled_job_registry.get_job_ids()
        return self.job_id in scheduled_jobs

    def save(self, **kwargs):
        schedule_job = kwargs.pop('schedule_job', True)
        super(BaseJob, self).save(**kwargs)
        if schedule_job:
            self.schedule()
            super(BaseJob, self).save()

    def delete(self, **kwargs):
        self.unschedule()
        super(BaseJob, self).delete(**kwargs)

    @admin.display(description='Callable')
    def function_string(self) -> str:
        func = self.callable + "(\u200b{})"  # zero-width space allows textwrap
        args = self.parse_args()
        args_list = [repr(arg) for arg in args]
        kwargs = self.parse_kwargs()
        kwargs_list = [k + '=' + repr(v) for (k, v) in kwargs.items()]
        return func.format(', '.join(args_list + kwargs_list))

    def parse_args(self):
        args = self.callable_args.all()
        return [arg.value() for arg in args]

    def parse_kwargs(self):
        kwargs = self.callable_kwargs.all()
        return dict([kwarg.value() for kwarg in kwargs])

    def enqueue_args(self) -> dict:
        res = dict(
            meta=dict(
                repeat=self.repeat,
                job_type=self.JOB_TYPE,
            ),
            on_success=callback_save_job,
            on_failure=callback_save_job,
        )
        if self.at_front:
            res['at_front'] = self.at_front
        if self.timeout:
            res['job_timeout'] = self.timeout
        if self.result_ttl is not None:
            res['result_ttl'] = self.result_ttl
        return res

    def _get_rqueue(self):
        return get_queue(self.queue)

    def ready_for_schedule(self) -> bool:
        if self.is_scheduled():
            logger.debug(f'Job {self.name} already scheduled')
            return False
        if not self.enabled:
            logger.debug(f'Job {str(self)} disabled, enable job before scheduling')
            return False
        return True

    def schedule(self) -> bool:
        if not self.ready_for_schedule():
            return False
        schedule_time = self._schedule_time()
        kwargs = self.enqueue_args()
        job = self._get_rqueue().enqueue_at(
            schedule_time,
            tools.run_job,
            args=(self.JOB_TYPE, self.id),
            **kwargs, )
        self.job_id = job.id
        return True

    def unschedule(self) -> bool:
        queue = self._get_rqueue()
        if self.is_scheduled():
            queue.remove(self.job_id)
            queue.scheduled_job_registry.remove(self.job_id)
        self.job_id = None
        return True

    def _schedule_time(self):
        raise NotImplementedError

    class Meta:
        abstract = True


class ScheduledTimeMixin(models.Model):
    scheduled_time = models.DateTimeField(_('scheduled time'))

    def _schedule_time(self):
        return utc(self.scheduled_time)

    class Meta:
        abstract = True


class ScheduledJob(ScheduledTimeMixin, BaseJob):
    repeat = None
    JOB_TYPE = 'ScheduledJob'

    def ready_for_schedule(self) -> bool:
        if super(ScheduledJob, self).ready_for_schedule() is False:
            return False
        if self.scheduled_time is not None and self.scheduled_time < timezone.now():
            return False
        return True

    class Meta:
        verbose_name = _('Scheduled Job')
        verbose_name_plural = _('Scheduled Jobs')
        ordering = ('name',)


class RepeatableJob(ScheduledTimeMixin, BaseJob):
    UNITS = Choices(
        ('seconds', _('seconds')),
        ('minutes', _('minutes')),
        ('hours', _('hours')),
        ('days', _('days')),
        ('weeks', _('weeks')),
    )

    interval = models.PositiveIntegerField(_('interval'))
    interval_unit = models.CharField(
        _('interval unit'), max_length=12, choices=UNITS, default=UNITS.hours
    )
    JOB_TYPE = 'RepeatableJob'

    def clean(self):
        super(RepeatableJob, self).clean()
        self.clean_interval_unit()
        self.clean_result_ttl()

    def clean_interval_unit(self):
        if RQ_SCHEDULER_INTERVAL > self.interval_seconds():
            raise ValidationError(
                _("Job interval is set lower than %(queue)r queue's interval. "
                  "minimum interval is %(interval)"),
                code='invalid',
                params={'queue': self.queue, 'interval': RQ_SCHEDULER_INTERVAL})
        if self.interval_seconds() % RQ_SCHEDULER_INTERVAL:
            raise ValidationError(
                _("Job interval is not a multiple of rq_scheduler's interval frequency: %(interval)ss"),
                code='invalid',
                params={'interval': RQ_SCHEDULER_INTERVAL})

    def clean_result_ttl(self) -> None:
        """
        Throws an error if there are repeats left to run and the result_ttl won't last until the next scheduled time.
        :return: None
        """
        if self.result_ttl and self.result_ttl != -1 and self.result_ttl < self.interval_seconds() and self.repeat:
            raise ValidationError(
                _("Job result_ttl must be either indefinite (-1) or "
                  "longer than the interval, %(interval)s seconds, to ensure rescheduling."),
                code='invalid',
                params={'interval': self.interval_seconds()}, )

    def interval_display(self):
        return '{} {}'.format(self.interval, self.get_interval_unit_display())

    def interval_seconds(self):
        kwargs = {self.interval_unit: self.interval, }
        return timedelta(**kwargs).total_seconds()

    def _prevent_duplicate_runs(self):
        """
        Counts the number of repeats lapsed between scheduled time and now
        and decrements that amount from the repeats remaining and updates the scheduled time to the next repeat.

        self.repeat is None ==> Run forever.
        """

    def enqueue_args(self):
        res = super(RepeatableJob, self).enqueue_args()
        res['meta']['interval'] = self.interval_seconds()
        return res

    def ready_for_schedule(self):
        if super(RepeatableJob, self).ready_for_schedule() is False:
            return False
        if self.scheduled_time < timezone.now():
            gap = math.ceil((timezone.now().timestamp() - self.scheduled_time.timestamp()) / self.interval_seconds())
            if self.repeat is None or self.repeat >= gap:
                self.scheduled_time += timedelta(seconds=self.interval_seconds() * gap)
                self.repeat = (self.repeat - gap) if self.repeat is not None else None

        if self.scheduled_time < timezone.now():
            return False
        return True

    class Meta:
        verbose_name = _('Repeatable Job')
        verbose_name_plural = _('Repeatable Jobs')
        ordering = ('name',)


class CronJob(BaseJob):
    JOB_TYPE = 'CronJob'

    cron_string = models.CharField(
        _('cron string'), max_length=64,
        help_text=_('Define the schedule in a crontab like syntax. Times are in UTC.')
    )

    def clean(self):
        super(CronJob, self).clean()
        self.clean_cron_string()

    def clean_cron_string(self):
        try:
            croniter.croniter(self.cron_string)
        except ValueError as e:
            raise ValidationError({
                'cron_string': ValidationError(
                    _(str(e)), code='invalid')
            })

    def _schedule_time(self):
        return tools.get_next_cron_time(self.cron_string)

    class Meta:
        verbose_name = _('Cron Job')
        verbose_name_plural = _('Cron Jobs')
        ordering = ('name',)