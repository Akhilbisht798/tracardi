from collections import defaultdict
from time import time
from typing import Dict, List, Tuple

from pydantic import ValidationError
from tracardi_graph_runner.domain.debug_info import DebugInfo
from tracardi_graph_runner.domain.error_debug_info import ErrorDebugInfo
from tracardi_graph_runner.domain.flow_debug_info import FlowDebugInfo
from tracardi_graph_runner.domain.flow_history import FlowHistory
from tracardi_graph_runner.domain.work_flow import WorkFlow

from .dot_accessor import DotAccessor
from ..domain.entity import Entity
from ..domain.flow import Flow
from ..domain.metadata import Metadata
from ..domain.payload.event_payload import EventPayload
from ..domain.profile import Profile, Profiles
from ..domain.record.event_debug_record import EventDebugRecord
from ..domain.session import Session
from ..domain.time import Time
from ..domain.value_object.operation import Operation
from ..event_server.service.persistence_service import PersistenceService
from ..event_server.utils.memory_cache import MemoryCache, CacheItem
from ..domain.event import Event
from ..domain.events import Events
from ..domain.rule import Rule
from ..domain.rules import Rules
from ..service.dot_notation_converter import DotNotationConverter
from ..service.storage.collection_crud import CollectionCrud
import asyncio
import traceback

import copy
from app.domain.segment import Segment
from app.domain.segments import Segments
from app.process_engine.tql.condition import Condition
from app.process_engine.tql.utils.dictonary import flatten
from ..service.storage.elastic_storage import ElasticStorage


class RulesEngine:

    def __init__(self,
                 session: Session,
                 profile: Profile,
                 events: Events
                 ):
        self.session = session
        self.profile = profile
        self.events = events

    @staticmethod
    async def load_rules(event_type: str):

        query = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "term": {
                                "event.type.keyword": event_type
                            }
                        },
                        {
                            "match": {
                                "enabled": True
                            }
                        }
                    ]
                }
            }
        }

        # todo set MemoryCache ttl from env
        memory_cache = MemoryCache()

        if 'rules' not in memory_cache:
            flows_data = await Rules.storage().filter(query)
            memory_cache['rules'] = CacheItem(data=flows_data, ttl=1)

        rules = list(memory_cache['rules'].data)
        return rules

    async def segmentation(self, event_types):

        # todo cache segments for 30 sec
        flat_payload = flatten(copy.deepcopy(self.profile.dict()))

        for event_type in event_types:  # type: str

            # Segmentation is run for every event

            # todo segments are loaded one by one - maybe it is possible to load it at once
            segments = await Segments.storage().load_by('eventType.keyword', event_type)
            for segment in segments:

                segment = Segment(**segment)
                segment_id = segment.get_id()

                try:

                    if Condition.evaluate(segment.condition, flat_payload):
                        segments = set(self.profile.segments)
                        segments.add(segment_id)
                        self.profile.segments = list(segments)

                        # Yield only if segmentation triggered
                        yield event_type, segment_id, None

                except Exception as e:
                    msg = 'Condition id `{}` could not evaluate `{}`. The following error was raised: `{}`'.format(
                        segment_id, segment.condition, str(e).replace("\n", " "))

                    yield event_type, segment_id, msg

    async def invoke(self, source_id=None) -> Tuple[Dict[str, List[Dict[str, DebugInfo]]], Dict[str, list]]:

        flow_task_store = defaultdict(list)
        results = defaultdict(list)

        for event in self.events:  # type: Event

            # Loads rules only for event.type
            rules = await RulesEngine.load_rules(event.type)

            for rule in rules:

                try:
                    rule = Rule(**rule)
                except ValidationError:
                    # todo log error and disable rule
                    continue

                if not rule.enabled:
                    continue

                try:
                    flow = await Flow.decode(rule.flow.id)
                except Exception as e:
                    result = DebugInfo(
                        timestamp=time(),
                        event=Entity(id=event.id),
                        flow=FlowDebugInfo(
                            id=rule.flow.id,
                            error=[ErrorDebugInfo(msg=str(e), file=__file__, line=103)]
                        )
                    )
                    results[event.type].append({rule.name: result})
                    continue

                if not flow.enabled:
                    continue

                # Validates rule source. Type was verified before

                if source_id:
                    if source_id == event.source.id:
                        # todo FlowHistory is empty
                        workflow = WorkFlow(
                            FlowHistory(history=[]),
                            self.session,
                            self.profile,
                            event
                        )
                        flow_task = asyncio.create_task(workflow.invoke(flow, debug=False))
                        flow_task_store[event.type].append((rule.flow.id, event.id, rule.name, flow_task))
                else:
                    # todo FlowHistory is empty
                    workflow = WorkFlow(
                        FlowHistory(history=[]),
                        self.session,
                        self.profile,
                        event
                    )
                    flow_task = asyncio.create_task(workflow.invoke(flow, debug=False))
                    flow_task_store[event.type].append((rule.flow.id, event.id, rule.name, flow_task))

        # Run and report async
        for event_type, tasks in flow_task_store.items():
            for flow_id, event_id, rule_name, task in tasks:
                # result = await task  # type: DebugInfo
                try:
                    result = await task  # type: DebugInfo
                except Exception as e:
                    # todo log error
                    result = DebugInfo(
                        timestamp=time(),
                        event=Entity(id=event_id),
                        flow=FlowDebugInfo(
                            id=flow_id,
                            error=[ErrorDebugInfo(msg=str(e), file=__file__, line=86)]
                        )
                    )

                results[event_type].append({rule_name: result})

        # Save profile
        # todo updated might be unnecessary - we could check if profile changed.
        segmentation_info = {
            "errors": [],
            "ids": []
        }
        if self.profile.operation.needs_segmentation():
            # Segmentation runs only if profile was updated or flow forced it
            async for event_type, segment_id, error in self.segmentation(event_types=flow_task_store.keys()):
                # Segmentation triggered
                if error:
                    segmentation_info['errors'].append(error)
                segmentation_info['ids'].append(segment_id)
            self.profile.operation.update = True

        print(self.profile.operation)
        print("merge", self.profile.operation.needs_merging())

        # todo merge
        if self.profile.operation.needs_merging():
            merge_key_values = self.profile.get_merge_key_values()

            # Add keyword
            merge_key_values = [(f"{field}.keyword", value) for field, value in merge_key_values]

            # Add only active profiles
            merge_key_values.append(('active', True))
            print(merge_key_values)

            profiles = await Operation.get_merge_profiles(merge_key_values)
            print("found", profiles)

            if len(profiles) > 0:

                # Get merged profile
                mergerd_profile = Profiles.merge(profiles)

                # Deactivate all profiles
                disabled_profiles = []
                for p in profiles:

                    p.active = False
                    p.mergedWith = [mergerd_profile.id]

                    disabled_profiles.append(p)

                print("new", mergerd_profile.id)
                print("disabled", [(x.id, x.active) for x in disabled_profiles])

                profiles = Profiles(disabled_profiles)
                save_old_profiles_task = asyncio.create_task(profiles.bulk().save())
                save_profile_task = asyncio.create_task(mergerd_profile.storage().save())

                await save_profile_task
                await save_old_profiles_task

        if self.profile.operation.needs_update():
            await self.profile.storage().save()

        return results, segmentation_info

    async def execute(self, source_id) -> Tuple[Dict[str, List[Dict[str, DebugInfo]]], Dict[str, list]]:

        """Runs rules and flow and saves debug info """

        flow_result_by_event, segmentation_info = await self.invoke(source_id)

        # Collect debug info
        record = []
        for debug_info_record in EventDebugRecord.encode(flow_result_by_event):  # type: EventDebugRecord
            record.append(debug_info_record)

        # Save in debug index
        bulk = CollectionCrud("debug-info", record)
        await bulk.save()

        return flow_result_by_event, segmentation_info

    @staticmethod
    async def raise_event(event_type, properties, session, profile, source_id):

        error_event = EventPayload(
            type=event_type,
            properties=properties,
        )

        error_event = error_event.to_event(
            Metadata(time=Time()),
            Entity(id=source_id),
            session,
            profile,
            {}
        )

        events = Events()
        events.append(error_event)

        save_events_task = asyncio.create_task(events.bulk().save())

        rules_engine = RulesEngine(session, profile, events)

        rules_engine_result = asyncio.create_task(rules_engine.execute(source_id))

        await save_events_task
        return await rules_engine_result
