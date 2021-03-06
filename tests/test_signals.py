#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tests for the ``django-user-tasks`` Celery signal handlers and Django signal.
"""

from __future__ import absolute_import, print_function, unicode_literals

import logging

import pytest
from celery import __version__ as celery_version
from celery import chain, chord, group, shared_task
from packaging import version

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from user_tasks import user_task_stopped
from user_tasks.models import UserTaskStatus
from user_tasks.tasks import UserTask

CELERY_VERSION = version.parse(celery_version)
LOGGER = logging.getLogger(__name__)
USER_ID = 1


class SampleTask(UserTask):  # pylint: disable=abstract-method
    """
    Small UserTask subclass for use in test cases.
    """

    @classmethod
    def generate_name(cls, arguments_dict):
        return 'SampleTask: {}'.format(arguments_dict['argument'])

    @staticmethod
    def calculate_total_steps(arguments_dict):
        return arguments_dict['kwargs'].get('total_steps', UserTask.calculate_total_steps(arguments_dict))


@shared_task(base=SampleTask, bind=True)
def sample_task(self, user_id, argument, **kwargs):  # pylint: disable=unused-argument
    """
    Example of a specific task inheriting from UserTask.
    """
    print('Ran SampleTask for argument "{}"'.format(argument))
    return argument


@shared_task(base=SampleTask, bind=True)
def missing_user_id(self, *args, **kwargs):  # pylint: disable=unused-argument
    """
    Example of a UserTask subclass which doesn't specifically require the mandatory user_id argument.
    """
    pass


@shared_task(bind=True)
def normal_task(self, *args, **kwargs):  # pylint: disable=unused-argument
    """
    Simple Celery task which doesn't inherit from UserTaskMixin.
    """
    return 'placeholder'


def verify_state(status, eager):
    """
    Assert that completed_steps, state, and attempts are correct for the given value of CELERY_ALWAYS_EAGER.
    """
    assert status.attempts == 1
    if eager:
        assert status.completed_steps == status.total_steps
        assert status.state == UserTaskStatus.SUCCEEDED
    else:
        assert status.completed_steps == 0
        assert status.state == UserTaskStatus.PENDING


SIGNAL_DATA = {}


def receiver(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Signal handler for testing the user_task_stopped signal.
    """
    if 'received_status' in SIGNAL_DATA:
        # Flag duplicate signals
        SIGNAL_DATA['received_status'].state += ' again'
    SIGNAL_DATA['received_status'] = kwargs.get('status')


user_task_stopped.connect(receiver)


class TestCreateUserTask(TestCase):
    """
    Tests of UserTaskStatus creation for new UserTasks.
    """

    def tearDown(self):
        super(TestCreateUserTask, self).tearDown()
        SIGNAL_DATA.clear()

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('test_user', 'test@example.com', 'password')

    def test_create_user_task(self):
        """The create_user_task signal handler should create a new UserTaskStatus record"""
        self._create_user_task(eager=False)

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_create_user_task_eager(self):
        """Eager tasks should still have UserTaskStatus records created on execution."""
        self._create_user_task(eager=True)
        assert SIGNAL_DATA['received_status'].state == UserTaskStatus.SUCCEEDED

    def test_create_group(self):
        """The create_user_task signal handler should correctly handle celery groups"""
        self._create_group(eager=False)

    @pytest.mark.skipif(
        CELERY_VERSION < version.parse('4.0'),
        reason="celery 3.1 doesn't provide accurate group metadata to eager tasks")
    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_create_group_eager(self):
        """Eager groups should still have UserTaskStatus records created on execution."""
        self._create_group(eager=True)

    def test_create_chain(self):
        """The create_user_task signal handler should correctly handle celery chains."""
        self._create_chain(eager=False)

    @pytest.mark.skipif(
        CELERY_VERSION < version.parse('4.0'),
        reason="celery 3.1 doesn't provide accurate chain metadata to eager tasks")
    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_create_chain_eager(self):
        """Eager chains should still have UserTaskStatus records created on execution."""
        self._create_chain(eager=True)

    def test_create_chord(self):
        """The create_user_task signal handler should correctly handle celery chords"""
        self._create_chord(eager=False)

    @pytest.mark.skipif(
        CELERY_VERSION < version.parse('4.0'),
        reason="celery 3.1 doesn't provide accurate chord metadata to eager tasks")
    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_create_chord_eager(self):
        """Eager chords should still have UserTaskStatus records created on execution."""
        self._create_chord(eager=True)

    def test_create_chord_exclude_body(self):
        """If the body task of a chord is not a UserTask, it should be cleanly omitted from the status."""
        chord([
            sample_task.s(self.user.id, '1', user_task_name='Chord: 1 & 2'),
            sample_task.s(self.user.id, '2', user_task_name='I should be ignored')
        ])(normal_task.s('3'))
        assert UserTaskStatus.objects.count() == 4
        chord_status = UserTaskStatus.objects.get(task_class='celery.chord')
        assert chord_status.task_id
        assert chord_status.parent is None
        assert chord_status.is_container
        assert chord_status.name == 'Chord: 1 & 2'
        assert chord_status.total_steps == 2
        verify_state(chord_status, False)

        group_status = UserTaskStatus.objects.get(task_class='celery.group')
        assert group_status.task_id
        assert group_status.parent_id == chord_status.id
        assert group_status.is_container
        assert group_status.name == 'Chord: 1 & 2'
        assert group_status.total_steps == 2
        verify_state(group_status, False)

        header_tasks = UserTaskStatus.objects.filter(parent=group_status)
        assert len(header_tasks) == 2
        for status in header_tasks:
            assert status.task_id
            assert status.parent_id == group_status.id
            assert not status.is_container
            assert status.name in ['SampleTask: 1', 'SampleTask: 2']
            assert status.total_steps == 1
            verify_state(status, False)

    def test_missing_user_id(self):
        """Queueing of the task should fail if the user ID is not provided."""
        with pytest.raises(TypeError) as exc_info:
            missing_user_id.delay()
        assert str(exc_info.value) == 'Each invocation of a UserTaskMixin subclass must include the user_id'

    def test_invalid_user_id(self):
        """Queueing of the task should fail if an invalid user ID is given."""
        with pytest.raises(TypeError) as exc_info:
            sample_task.delay('arg1', 'arg2')
        assert str(exc_info.value) == 'Invalid user_id: arg1'

    def test_non_user_task_publish(self):
        """Non-UserTask tasks should still pass through the before_task_publish handler cleanly."""
        normal_task.delay('Argument')
        statuses = UserTaskStatus.objects.all()
        assert not statuses

    @override_settings(CELERY_ALWAYS_EAGER=True, CELERY_IGNORE_RESULT=False)
    def test_non_user_task_success(self):
        """Non-UserTask tasks should still pass through start and success handlers cleanly."""
        result = normal_task.delay('Argument')
        assert result.get() == 'placeholder'
        statuses = UserTaskStatus.objects.all()
        assert not statuses

    def _create_user_task(self, eager):
        """Create a task based on UserTaskMixin and verify some assertions about its corresponding status."""
        result = sample_task.delay(self.user.id, 'Argument')
        statuses = UserTaskStatus.objects.all()
        assert len(statuses) == 1
        status = statuses[0]
        assert status.task_id == result.id
        assert status.task_class == 'test_signals.sample_task'
        assert status.user_id == self.user.id
        assert status.parent is None
        assert not status.is_container
        assert status.name == 'SampleTask: Argument'
        assert status.total_steps == 1
        verify_state(status, eager)

    def _create_chain(self, eager):
        """Create a celery chain and verify some assertions about the corresponding status records"""
        chain(sample_task.si(self.user.id, '1'),
              sample_task.si(self.user.id, '2', user_task_name='Chain: 1, 2, 3'),
              sample_task.si(self.user.id, '3'),
              normal_task.si('Argument')).delay()
        assert UserTaskStatus.objects.count() == 4
        chain_status = UserTaskStatus.objects.get(task_class='celery.chain')
        assert chain_status.task_id
        assert chain_status.parent is None
        assert chain_status.is_container
        assert chain_status.name == 'Chain: 1, 2, 3'
        assert chain_status.total_steps == 3
        verify_state(chain_status, eager)

        children = UserTaskStatus.objects.filter(parent=chain_status)
        assert len(children) == 3
        for status in children:
            assert not status.is_container
            assert status.name in ['SampleTask: 1', 'SampleTask: 2', 'SampleTask: 3']
            assert status.total_steps == 1
            verify_state(status, eager)

    def _create_chord(self, eager):
        """Create a celery chord and verify some assertions about the corresponding status records"""
        chord([
            sample_task.s(self.user.id, '1'),
            sample_task.s(self.user.id, '2', user_task_name='Chord: 1 & 2, then 3')
        ])(sample_task.s(self.user.id, '3'))
        assert UserTaskStatus.objects.count() == 5
        chord_status = UserTaskStatus.objects.get(task_class='celery.chord')
        assert chord_status.task_id
        assert chord_status.parent is None
        assert chord_status.is_container
        assert chord_status.name == 'Chord: 1 & 2, then 3'
        assert chord_status.total_steps == 3
        verify_state(chord_status, eager)

        group_status = UserTaskStatus.objects.get(task_class='celery.group')
        assert group_status.task_id
        assert group_status.parent_id == chord_status.id
        assert group_status.is_container
        assert group_status.name == 'Chord: 1 & 2, then 3'
        assert group_status.total_steps == 2
        verify_state(group_status, eager)

        header_tasks = UserTaskStatus.objects.filter(parent=group_status)
        assert len(header_tasks) == 2
        for status in header_tasks:
            assert status.task_id
            assert status.parent_id == group_status.id
            assert not status.is_container
            assert status.name in ['SampleTask: 1', 'SampleTask: 2']
            assert status.total_steps == 1
            verify_state(status, eager)

        body_status = UserTaskStatus.objects.get(parent=chord_status, is_container=False)
        assert body_status.task_id
        assert body_status.name == 'SampleTask: 3'
        assert body_status.total_steps == 1
        verify_state(body_status, eager)

    def _create_group(self, eager):
        """Create a celery group and verify some assertions about the corresponding status records"""
        result = group(sample_task.s(self.user.id, '1'),
                       sample_task.s(self.user.id, '2', user_task_name='Group: 1, 2')).delay()
        assert UserTaskStatus.objects.count() == 3
        group_status = UserTaskStatus.objects.get(task_class='celery.group')
        assert group_status.task_id == result.id
        assert group_status.parent is None
        assert group_status.is_container
        assert group_status.name == 'Group: 1, 2'
        assert group_status.total_steps == 2
        verify_state(group_status, eager)

        assert len(result.children) == 2
        for result in result.children:
            task_id = result.id
            status = UserTaskStatus.objects.get(task_id=task_id)
            assert status.parent_id == group_status.id
            assert not status.is_container
            assert status.name in ['SampleTask: 1', 'SampleTask: 2']
            assert status.total_steps == 1
            verify_state(status, eager)


@shared_task(base=SampleTask, bind=True)
def failing_task(self, user_id, argument, **kwargs):  # pylint: disable=unused-argument
    """
    A task using the UserTask framework which always throws an exception.
    """
    raise Exception('Boom!')


@shared_task(base=SampleTask, bind=True)
def manually_failed_task(self, user_id, argument, **kwargs):  # pylint: disable=unused-argument
    """
    A task using the UserTask framework which fails without throwing an exception.
    """
    self.status.fail("Something went wrong")


@shared_task(bind=True)
def normal_failing_task(self, *args, **kwargs):  # pylint: disable=unused-argument
    """
    A non-UserTaskMixin Celery task which always throws an exception.
    """
    raise Exception('Boom!')


@shared_task(base=SampleTask, bind=True)
def retried_task(self, user_id, argument, **kwargs):  # pylint: disable=unused-argument
    """
    A task using the UserTask framework which fails once and succeeds on the retry.
    """
    if self.request.retries == 0:
        try:
            raise Exception('Boom!')
        except Exception as exc:
            raise self.retry(exc=exc)
    return argument


@shared_task(bind=True)
def normal_retried_task(self, *args, **kwargs):  # pylint: disable=unused-argument
    """
    A non-UserTaskMixin Celery task which fails once and succeeds on the retry.
    """
    if self.request.retries == 0:
        try:
            raise Exception('Boom!')
        except Exception as exc:
            raise self.retry(exc=exc)
    return 'placeholder'


@shared_task(base=SampleTask, bind=True)
def self_canceled_task(self, user_id, argument, **kwargs):  # pylint: disable=unused-argument
    """
    This task cancels itself to simulate a task canceled during execution.
    """
    status = self.status
    status.state = UserTaskStatus.CANCELED
    status.save()
    self.status.increment_completed_steps()


class TestStatusChanges(TestCase):
    """
    Tests of signals indicating changes in the task's status.
    """

    def tearDown(self):
        super(TestStatusChanges, self).tearDown()
        SIGNAL_DATA.clear()

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('test_user', 'test@example.com', 'password')

    def test_canceled_before_execution(self):
        """A UserTask which is canceled before execution should have its status updated accordingly."""
        result = sample_task.delay(self.user.id, 'Argument')
        statuses = UserTaskStatus.objects.all()
        assert len(statuses) == 1
        status = statuses[0]
        status.cancel()
        assert status.task_id == result.id
        assert status.task_class == 'test_signals.sample_task'
        assert status.user_id == self.user.id
        assert status.parent is None
        assert not status.is_container
        assert status.name == 'SampleTask: Argument'
        assert status.total_steps == 1
        assert status.completed_steps == 0
        assert status.state == UserTaskStatus.CANCELED
        assert status.attempts == 1
        assert SIGNAL_DATA['received_status'].state == UserTaskStatus.CANCELED

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_canceled_during_execution(self):
        """A UserTask which is canceled during execution should have its status updated accordingly."""
        result = self_canceled_task.delay(self.user.id, 'Argument', total_steps=3)
        statuses = UserTaskStatus.objects.all()
        assert len(statuses) == 1
        status = statuses[0]
        assert status.task_id == result.id
        assert status.task_class == 'test_signals.self_canceled_task'
        assert status.user_id == self.user.id
        assert status.parent is None
        assert not status.is_container
        assert status.name == 'SampleTask: Argument'
        assert status.total_steps == 3
        assert status.completed_steps == 1
        assert status.state == UserTaskStatus.CANCELED
        assert status.attempts == 1
        assert SIGNAL_DATA['received_status'].state == UserTaskStatus.CANCELED

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_failed(self):
        """A UserTask that failed should have its status updated accordingly."""
        result = failing_task.delay(self.user.id, 'Argument')
        statuses = UserTaskStatus.objects.all()
        assert len(statuses) == 1
        status = statuses[0]
        assert status.task_id == result.id
        assert status.task_class == 'test_signals.failing_task'
        assert status.user_id == self.user.id
        assert status.parent is None
        assert not status.is_container
        assert status.name == 'SampleTask: Argument'
        assert status.total_steps == 1
        assert status.completed_steps == 0
        assert status.state == UserTaskStatus.FAILED
        assert status.attempts == 1
        assert SIGNAL_DATA['received_status'].state == UserTaskStatus.FAILED

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_manually_failed(self):
        """A UserTask that failed cleanly (no exception) should have its status updated accordingly."""
        result = manually_failed_task.delay(self.user.id, 'Argument')
        statuses = UserTaskStatus.objects.all()
        assert len(statuses) == 1
        status = statuses[0]
        assert status.task_id == result.id
        assert status.task_class == 'test_signals.manually_failed_task'
        assert status.user_id == self.user.id
        assert status.parent is None
        assert not status.is_container
        assert status.name == 'SampleTask: Argument'
        assert status.total_steps == 1
        assert status.completed_steps == 0
        assert status.state == UserTaskStatus.FAILED
        assert status.attempts == 1
        assert SIGNAL_DATA['received_status'].state == UserTaskStatus.FAILED

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_retried(self):
        """A UserTask that is to be retried should have its status updated accordingly."""
        result = retried_task.delay(self.user.id, 'Argument')
        statuses = UserTaskStatus.objects.all()
        assert len(statuses) == 1
        status = statuses[0]
        assert status.task_id == result.id
        assert status.task_class == 'test_signals.retried_task'
        assert status.user_id == self.user.id
        assert status.parent is None
        assert not status.is_container
        assert status.name == 'SampleTask: Argument'
        # Eager task execution sends the retry signal last; odd, but makes testing easier
        assert status.state == UserTaskStatus.RETRYING
        assert status.total_steps == 1
        assert status.completed_steps == 1
        assert status.attempts == 2

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_non_user_task_success(self):
        """Non-UserTask tasks should still pass through the failure handler cleanly."""
        normal_failing_task.delay('Argument')
        statuses = UserTaskStatus.objects.all()
        assert not statuses
        assert 'received_status' not in SIGNAL_DATA

    @override_settings(CELERY_ALWAYS_EAGER=True)
    def test_non_user_task_retry(self):
        """Non-UserTask tasks should still pass through the retry handler cleanly."""
        normal_retried_task.delay('Argument')
        statuses = UserTaskStatus.objects.all()
        assert not statuses
        assert 'received_status' not in SIGNAL_DATA

    def test_duplicate_stopped_signal(self):
        """The test signal receiver should be able to detect and flag duplicate signals"""
        sample_task.delay(self.user.id, 'Argument')
        status = UserTaskStatus.objects.all()[0]
        user_task_stopped.send_robust(sender=UserTaskStatus, status=status)
        status.cancel()
        user_task_stopped.send_robust(sender=UserTaskStatus, status=status)
        assert SIGNAL_DATA['received_status'].state == UserTaskStatus.CANCELED + ' again'
