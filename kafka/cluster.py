from __future__ import absolute_import

import logging
import random
import time

import kafka.common as Errors
from kafka.common import BrokerMetadata
from .future import Future

log = logging.getLogger(__name__)


class ClusterMetadata(object):
    _retry_backoff_ms = 100
    _metadata_max_age_ms = 300000

    def __init__(self, **kwargs):
        self._brokers = {}
        self._partitions = {}
        self._groups = {}
        self._version = 0
        self._last_refresh_ms = 0
        self._last_successful_refresh_ms = 0
        self._need_update = False
        self._future = None
        self._listeners = set()

        for config in ('retry_backoff_ms', 'metadata_max_age_ms'):
            if config in kwargs:
                setattr(self, '_' + config, kwargs.pop(config))

    def brokers(self):
        return set(self._brokers.values())

    def broker_metadata(self, broker_id):
        return self._brokers.get(broker_id)

    def partitions_for_topic(self, topic):
        if topic not in self._partitions:
            return None
        return set(self._partitions[topic].keys())

    def leader_for_partition(self, partition):
        if partition.topic not in self._partitions:
            return None
        return self._partitions[partition.topic].get(partition.partition)

    def coordinator_for_group(self, group):
        return self._groups.get(group)

    def ttl(self):
        """Milliseconds until metadata should be refreshed"""
        now = time.time() * 1000
        if self._need_update:
            ttl = 0
        else:
            ttl = self._last_successful_refresh_ms + self._metadata_max_age_ms - now
        retry = self._last_refresh_ms + self._retry_backoff_ms - now
        return max(ttl, retry, 0)

    def request_update(self):
        """
        Flags metadata for update, return Future()

        Actual update must be handled separately. This method will only
        change the reported ttl()
        """
        self._need_update = True
        if not self._future or self._future.is_done:
          self._future = Future()
        return self._future

    def topics(self):
        return set(self._partitions.keys())

    def failed_update(self, exception):
        if self._future:
            self._future.failure(exception)
            self._future = None
        self._last_refresh_ms = time.time() * 1000

    def update_metadata(self, metadata):
        # In the common case where we ask for a single topic and get back an
        # error, we should fail the future
        if len(metadata.topics) == 1 and metadata.topics[0][0] != 0:
            error_code, topic, _ = metadata.topics[0]
            error = Errors.for_code(error_code)(topic)
            return self.failed_update(error)

        if not metadata.brokers:
            log.warning("No broker metadata found in MetadataResponse")

        for node_id, host, port in metadata.brokers:
            self._brokers.update({
                node_id: BrokerMetadata(node_id, host, port)
            })

        # Drop any UnknownTopic, InvalidTopic, and TopicAuthorizationFailed
        # but retain LeaderNotAvailable because it means topic is initializing
        self._partitions = {}

        for error_code, topic, partitions in metadata.topics:
            error_type = Errors.for_code(error_code)
            if error_type is Errors.NoError:
                self._partitions[topic] = {}
                for _, partition, leader, _, _ in partitions:
                    self._partitions[topic][partition] = leader
            elif error_type is Errors.LeaderNotAvailableError:
                log.error("Topic %s is not available during auto-create"
                          " initialization", topic)
            elif error_type is Errors.UnknownTopicOrPartitionError:
                log.error("Topic %s not found in cluster metadata", topic)
            elif error_type is Errors.TopicAuthorizationFailedError:
                log.error("Topic %s is not authorized for this client", topic)
            elif error_type is Errors.InvalidTopicError:
                log.error("'%s' is not a valid topic name", topic)
            else:
                log.error("Error fetching metadata for topic %s: %s",
                          topic, error_type)

        if self._future:
            self._future.success(self)
        self._future = None
        self._need_update = False
        self._version += 1
        now = time.time() * 1000
        self._last_refresh_ms = now
        self._last_successful_refresh_ms = now
        log.debug("Updated cluster metadata version %d to %s",
                  self._version, self)

        for listener in self._listeners:
            listener(self)

    def add_listener(self, listener):
        """Add a callback function to be called on each metadata update"""
        self._listeners.add(listener)

    def remove_listener(self, listener):
        """Remove a previously added listener callback"""
        self._listeners.remove(listener)

    def __str__(self):
        return 'Cluster(brokers: %d, topics: %d, groups: %d)' % \
               (len(self._brokers), len(self._partitions), len(self._groups))
