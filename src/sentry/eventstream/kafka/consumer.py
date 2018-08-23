from __future__ import absolute_import

import functools
import logging
import threading
import uuid

from confluent_kafka import Consumer, OFFSET_BEGINNING, TopicPartition

from sentry.eventstream.kafka.state import SynchronizedPartitionState, SynchronizedPartitionStateManager
from sentry.utils.concurrent import execute


logger = logging.getLogger(__name__)


def get_commit_data(message):
    topic, partition, group = message.key().decode('utf-8').split(':', 3)
    partition = int(partition)
    offset = int(message.value().decode('utf-8'))
    return group, topic, partition, offset


def run_commit_log_consumer(bootstrap_servers, consumer_group, commit_log_topic,
                            partition_state_manager, synchronize_commit_group, stop_request_event):
    logging.debug('Starting commit log consumer...')

    positions = {}

    # NOTE: The commit log consumer group should not be persisted into the
    # ``__consumer_offsets`` topic since no offsets are committed by this
    # consumer. The group membership metadata messages will be published
    # initially but as long as this group remains a single consumer it will
    # be deleted after the consumer is closed.
    # It is very important to note that the ``group.id`` **MUST** be unique to
    # this consumer process!!! This ensures that it is able to consume from all
    # partitions of the commit log topic and get a comprehensive view of the
    # state of the consumer groups it is tracking.
    consumer = Consumer({
        'bootstrap.servers': bootstrap_servers,
        'group.id': consumer_group,
        'enable.auto.commit': 'false',
        'enable.auto.offset.store': 'true',
        'enable.partition.eof': 'false',
        'default.topic.config': {
            'auto.offset.reset': 'error',
        },
    })

    def rewind_partitions_on_assignment(consumer, assignment):
        # The commit log consumer must start consuming from the beginning of
        # the commit log topic to ensure that it has a comprehensive view of
        # all active partitions.
        consumer.assign([
            TopicPartition(
                i.topic,
                i.partition,
                positions.get((i.topic, i.partition), OFFSET_BEGINNING),
            ) for i in assignment
        ])

    consumer.subscribe(
        [commit_log_topic],
        on_assign=rewind_partitions_on_assignment,
    )

    while not stop_request_event.is_set():
        message = consumer.poll(1)
        if message is None:
            continue

        error = message.error()
        if error is not None:
            raise Exception(error)

        positions[(message.topic(), message.partition())] = message.offset() + 1

        group, topic, partition, offset = get_commit_data(message)
        if group != synchronize_commit_group:
            logger.debug('Received consumer offsets update from %r, ignoring...', group)
            continue

        partition_state_manager.set_remote_offset(topic, partition, offset)


def get_earliest_offset(consumer, topic, partition):
    low, high = consumer.get_watermark_offsets(TopicPartition(topic, partition))
    return low


def get_latest_offset(consumer, topic, partition):
    low, high = consumer.get_watermark_offsets(TopicPartition(topic, partition))
    return high


class SynchronizedConsumer(object):
    """
    This class implements the framework for a consumer that is intended to only
    consume messages that have already been consumed and committed by members
    of another consumer group.

    This works similarly to the Kafka built-in ``__consumer_offsets`` topic.
    The consumer group that is being "followed" (the one that must make
    progress for our consumer here to make progress, identified by the
    ``synchronize_commit_group`` constructor parameter/instance attribute) must
    report its offsets to a topic (identified by the ``commit_log_topic``
    constructor parameter/instance attribute). This consumer subscribes to both
    commit log topic, as well as the topic(s) that we are actually interested
    in consuming messages from. The messages received from the commit log topic
    control whether or not consumption from partitions belonging to the main
    topic is paused, resumed, or allowed to continue in its current state
    without changes.

    The furthest point in any partition that this consumer should ever consume
    to is the maximum offset that has been recorded to the commit log topic for
    that partition. If the offsets recorded to that topic move
    non-monotonically (due to an intentional offset rollback, for instance)
    this consumer *may* consume up to the highest watermark point. (The
    implementation here tries to pause consuming from the partition as soon as
    possible, but this makes no explicit guarantees about that behavior.)
    """
    initial_offset_reset_strategies = {
        'earliest': get_earliest_offset,
        'latest': get_latest_offset,
    }

    def __init__(self, bootstrap_servers, consumer_group, commit_log_topic,
                 synchronize_commit_group, initial_offset_reset='latest', on_commit=None):
        self.bootstrap_servers = bootstrap_servers
        self.consumer_group = consumer_group
        self.commit_log_topic = commit_log_topic
        self.synchronize_commit_group = synchronize_commit_group
        self.initial_offset_reset = self.initial_offset_reset_strategies[initial_offset_reset]

        self.__partition_state_manager = SynchronizedPartitionStateManager(
            self.__on_partition_state_change)
        self.__commit_log_consumer, self__commit_log_consumer_stop_request = self.__start_commit_log_consumer()

        self.__positions = {}

        def commit_callback(error, partitions):
            if on_commit is not None:
                return on_commit(error, partitions)

        consumer_configuration = {
            'bootstrap.servers': self.bootstrap_servers,
            'group.id': self.consumer_group,
            'enable.auto.commit': 'false',
            'enable.auto.offset.store': 'true',
            'enable.partition.eof': 'false',
            'default.topic.config': {
                'auto.offset.reset': 'error',
            },
            'on_commit': commit_callback,
        }

        self.__consumer = Consumer(consumer_configuration)

    def __start_commit_log_consumer(self):
        """
        Starts running the commit log consumer.
        """
        stop_request_event = threading.Event()
        return execute(
            functools.partial(
                run_commit_log_consumer,
                bootstrap_servers=self.bootstrap_servers,
                consumer_group='{}:sync:{}'.format(self.consumer_group, uuid.uuid1().hex),
                commit_log_topic=self.commit_log_topic,
                synchronize_commit_group=self.synchronize_commit_group,
                partition_state_manager=self.__partition_state_manager,
                stop_request_event=stop_request_event,
            ),
        ), stop_request_event

    def __on_partition_state_change(
            self, topic, partition, previous_state_and_offsets, current_state_and_offsets):
        """
        Callback that is invoked when a partition state changes.
        """
        logger.debug('State change for %r: %r to %r', (topic, partition),
                     previous_state_and_offsets, current_state_and_offsets)

        current_state, current_offsets = current_state_and_offsets
        if current_offsets.local is None:
            # It only makes sense to manipulate the consumer if we've got an
            # assignment. (This block should only be entered at startup if the
            # remote offsets are retrieved from the commit log before the local
            # consumer has received its assignment.)
            return

        # TODO: This wil be called from the commit log consumer thread, so need
        # to verify that calling the ``consumer.{pause,resume}`` methods is
        # thread safe!
        if current_state in (SynchronizedPartitionState.UNKNOWN, SynchronizedPartitionState.SYNCHRONIZED,
                             SynchronizedPartitionState.REMOTE_BEHIND):
            self.__consumer.pause([TopicPartition(topic, partition, current_offsets.local)])
        elif current_state is SynchronizedPartitionState.LOCAL_BEHIND:
            self.__consumer.resume([TopicPartition(topic, partition, current_offsets.local)])
        else:
            raise NotImplementedError('Unexpected partition state: %s' % (current_state,))

    def subscribe(self, topics, on_assign=None, on_revoke=None):
        """
        Subscribe to a topic.
        """
        def assignment_callback(consumer, assignment):
            # Since ``auto.offset.reset`` is set to ``error`` to force human
            # interaction on an offset reset, we have to explicitly specify the
            # starting offset if no offset has been committed for this topic during
            # the ``__consumer_offsets`` topic retention period.
            assignment = {
                (i.topic, i.partition): self.__positions.get((i.topic, i.partition)) for i in assignment
            }

            for i in self.__consumer.committed([TopicPartition(topic, partition) for (
                    topic, partition), offset in assignment.items() if offset is None]):
                k = (i.topic, i.partition)
                if i.offset > -1:
                    assignment[k] = i.offset
                else:
                    assignment[k] = self.initial_offset_reset(consumer, i.topic, i.partition)

            self.__consumer.assign([TopicPartition(topic, partition, offset)
                                    for (topic, partition), offset in assignment.items()])

            for (topic, partition), offset in assignment.items():
                # Setting the local offsets will either cause the partition to be
                # paused (if the remote offset is unknown or the local offset is
                # not trailing the remote offset) or resumed.
                self.__partition_state_manager.set_local_offset(topic, partition, offset)
                self.__positions[(topic, partition)] = offset

            if on_assign is not None:
                on_assign(self, [TopicPartition(topic, partition)
                                 for topic, partition in assignment.keys()])

        def revocation_callback(consumer, assignment):
            if on_revoke is not None:
                on_revoke(self, assignment)

        self.__consumer.subscribe(
            topics,
            on_assign=assignment_callback,
            on_revoke=revocation_callback)

    def poll(self, timeout):
        if not self.__commit_log_consumer.running():
            result = self.__commit_log_consumer.result(timeout=0)  # noqa
            raise Exception('Commit log consumer unexpectedly exit!')

        message = self.__consumer.poll(timeout)
        if message is None:
            return

        if message.error() is not None:
            return message

        self.__partition_state_manager.validate_local_message(
            message.topic(), message.partition(), message.offset())
        self.__partition_state_manager.set_local_offset(
            message.topic(), message.partition(), message.offset() + 1)
        self.__positions[(message.topic(), message.partition())] = message.offset() + 1

        return message

    def commit(self, *args, **kwargs):
        return self.__consumer.commit(*args, **kwargs)

    def close(self):
        self.__commit_log_consumer_stop_request.set()
        try:
            self.__consumer.close()
        finally:
            self.__commit_log_consumer.result()
