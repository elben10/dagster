import json
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import toposort

from dagster._annotations import experimental
from dagster._core.storage.pipeline_run import IN_PROGRESS_RUN_STATUSES, RunsFilter

from .asset_selection import AssetSelection
from .events import AssetKey
from .run_request import RunRequest
from .sensor_definition import DefaultSensorStatus, MultiAssetSensorDefinition, SensorDefinition
from .utils import check_valid_name

if TYPE_CHECKING:
    from dagster._core.definitions import AssetsDefinition, SourceAsset
    from dagster._core.storage.event_log.base import EventLogRecord


def _get_upstream_mapping(
    selection,
    assets,
    source_assets,
) -> Mapping[AssetKey, Set[AssetKey]]:
    """Computes a mapping of assets in self._selection to their parents in the asset graph"""
    upstream = defaultdict(set)
    selection_resolved = list(selection.resolve([*assets, *source_assets]))
    for a in selection_resolved:
        a_parents = list(
            AssetSelection.keys(a).upstream(depth=1).resolve([*assets, *source_assets])
        )
        # filter out a because upstream() includes the assets in the original AssetSelection
        upstream[a] = {p for p in a_parents if p != a}
    return upstream


def _get_parent_updates(
    context,
    current_asset: AssetKey,
    parent_assets: Set[AssetKey],
    cursor_timestamp: float,
    will_materialize_list: Sequence[AssetKey],
    wait_for_in_progress_runs: bool,
) -> Mapping[AssetKey, Tuple[bool, Optional[int]]]:
    """The bulk of the logic in the sensor is in this function. At the end of the function we return a
    dictionary that maps each asset to a Tuple. The Tuple contains a boolean, indicating if the asset
    has materialized or will materialize, and a float representing the timestamp the parent asset
    would update the cursor too if it is the most recent materialization of a parent asset. In some cases
    we set the timestamp to 0.0 so that the timestamps of other parent materializations will take precedent.

    Here's how we get there:

    We want to get the materialization information for all of the parents of an asset to determine
    if we want to materialize the asset in this sensor tick. We also need determine the new cursor
    value for the asset so that we don't process the same materialization events for the parent
    assets again.

    We iterate through each parent of the asset and determine its materialization info. The parent
    asset's materialization status can be one of three options:
    1. The parent has materialized since the last time the child was materialized (determined by comparing
        the timestamp of the parent materialization to the cursor_timestamp).
    2. The parent will be materialized as part of the materialization that will be kicked off by the
        sensor.
    3. The parent has not been materialized and will not be materialized by the sensor.

    In cases 1 and 2 we indicate that the parent has been updated by setting its value in
    parent_asset_event_records to True. For case 3 we set its value to False.

    If wait_for_in_progress_runs=True, there is another condition we want to check for.
    If any of the parents is currently being materialized we want to wait to materialize current_asset
    until the parent materialization is complete so that the asset can have the most up to date data.
    So, for each parent asset we check if it has a planned asset materialization event in a run that
    is currently in progress. If this is the case, we don't want current_asset to materialize, so we
    set parent_asset_event_records to False for all parents (so that if the sensor is set to
    materialize if any of the parents are updated, the sensor will still choose to not materialize
    the asset) and immediately return.
    """
    from dagster._core.events import DagsterEventType
    from dagster._core.storage.event_log.base import EventRecordsFilter

    parent_asset_event_records = {}

    for p in parent_assets:
        if p in will_materialize_list:
            # if p will be materialized by this sensor, then we can also materialize current_asset
            # we don't know what time asset p will be materialized so we set the cursor val to 0.0
            parent_asset_event_records[p] = (
                True,
                0.0,
            )
        # TODO - when source asset versioning lands, add a check here that will see if the version has
        # updated if p is a source asset
        else:
            if wait_for_in_progress_runs:
                # if p is currently being materialized, then we don't want to materialize current_asset

                # get the most recent planned materialization
                materialization_planned_event_records = context.instance.get_event_records(
                    EventRecordsFilter(
                        event_type=DagsterEventType.ASSET_MATERIALIZATION_PLANNED,
                        asset_key=p,
                    ),
                    ascending=False,
                    limit=1,
                )

                if materialization_planned_event_records:
                    # see if the most recent planned materialization is part of an in progress run
                    in_progress = context.instance.get_runs(
                        filters=RunsFilter(
                            run_ids=[
                                materialization_planned_event_records[0].event_log_entry.run_id
                            ],
                            statuses=IN_PROGRESS_RUN_STATUSES,
                        )
                    )
                    if in_progress:
                        # we don't want to materialize current_asset because p is
                        # being materialized. We'll materialize the asset on the next tick when the
                        # materialization of p is complete
                        parent_asset_event_records = {pp: (False, 0.0) for pp in parent_assets}

                        return parent_asset_event_records
            # check if there is a completed materialization for p
            event_records = context.instance.get_event_records(
                EventRecordsFilter(
                    event_type=DagsterEventType.ASSET_MATERIALIZATION,
                    asset_key=p,
                ),
                ascending=False,
                limit=1,
            )

            if event_records and event_records[0].event_log_entry.timestamp > cursor_timestamp:
                # if the run for the materialization of p also materialized current_asset, we
                # don't consider p "updated" when determining if current_asset should materialize
                other_materialized_asset_records = context.instance.get_records_for_run(
                    run_id=event_records[0].event_log_entry.run_id,
                    of_type=DagsterEventType.ASSET_MATERIALIZATION_PLANNED,
                ).records
                other_materialized_assets = [
                    event.event_log_entry.dagster_event.event_specific_data.asset_key
                    for event in other_materialized_asset_records
                ]
                if current_asset in other_materialized_assets:
                    # we still update the cursor for p so this materialization isn't considered
                    # on the next sensor tick
                    parent_asset_event_records[p] = (
                        False,
                        event_records[0].event_log_entry.timestamp,
                    )
                else:
                    # current_asset was not updated along with p, so we consider p updated
                    parent_asset_event_records[p] = (
                        True,
                        event_records[0].event_log_entry.timestamp,
                    )
            else:
                # p has not been materialized and will not be materialized by the sensor
                parent_asset_event_records[p] = (False, 0.0)

    return parent_asset_event_records


def _make_sensor(
    selection: AssetSelection,
    name: str,
    and_condition: bool,  # TODO better name for this parameter
    wait_for_in_progress_runs: bool,
    minimum_interval_seconds: Optional[int] = None,
    description: Optional[str] = None,
    default_status: DefaultSensorStatus = DefaultSensorStatus.STOPPED,
) -> SensorDefinition:
    """Creates the sensor that will monitor the parents of all provided assets and determine
    which assets should be materialized (ie their parents have been updated).

    The cursor for this sensor is a dictionary mapping stringified AssetKeys to a timestamp (float). For each
    asset we keep track of the timestamp of the most recent materialization of a parent asset. For example
    if asset X has parents A, B, and C where A was materialized at time 1, B at time 2 and C at time 3. When
    the sensor runs, the cursor for X will be set to 3. This way, the next time the sensor runs, we can ignore
    the materializations prior to time 3. If asset A materialized again at time 4, we would know that this materialization
    has not been incorporated into the child asset yet.
    """

    def sensor_fn(context):
        asset_defs_by_key = context._repository_def._assets_defs_by_key
        source_asset_defs_by_key = context._repository_def.source_assets_by_key
        upstream: Mapping[AssetKey, Set[AssetKey]] = _get_upstream_mapping(
            selection=selection,
            assets=asset_defs_by_key.values(),
            source_assets=source_asset_defs_by_key.values(),
        )

        cursor_dict: Dict[str, float] = json.loads(context.cursor) if context.cursor else {}
        should_materialize: List[AssetKey] = []
        cursor_update_dict: Dict[str, float] = {}

        # sort the assets topologically so that we process them in order
        toposort_assets = list(toposort.toposort(upstream))
        # unpack the list of sets into a list and only keep the ones we are monitoring
        toposort_assets = [
            asset for layer in toposort_assets for asset in layer if asset in upstream.keys()
        ]

        # determine which assets should materialize based on the materialization status of their
        # parents
        for a in toposort_assets:
            a_cursor = cursor_dict.get(str(a), 0.0)
            cursor_update_dict[str(a)] = a_cursor
            parent_update_records = _get_parent_updates(
                context,
                current_asset=a,
                parent_assets=upstream[a],
                cursor_timestamp=a_cursor,
                will_materialize_list=should_materialize,
                wait_for_in_progress_runs=wait_for_in_progress_runs,
            )

            condition = all if and_condition else any
            if condition(
                [
                    materialization_status
                    for materialization_status, _ in parent_update_records.values()
                ]
            ):
                should_materialize.append(a)
                cursor_update_dict[str(a)] = max(
                    [cursor_val for _, cursor_val in parent_update_records.values()] + [a_cursor]
                )

        if len(should_materialize) > 0:
            context.update_cursor(json.dumps(cursor_update_dict))
            context.cursor_has_been_updated = True
            return RunRequest(run_key=f"{context.cursor}", asset_selection=should_materialize)

    return MultiAssetSensorDefinition(
        asset_keys=[],
        asset_materialization_fn=sensor_fn,
        name=name,
        job_name="__ASSET_JOB",
        minimum_interval_seconds=minimum_interval_seconds,
        description=description,
        default_status=default_status,
    )


@experimental
def build_asset_reconciliation_sensor(
    selection: AssetSelection,
    name: str,
    and_condition: bool = True,  # TODO better name for this parameter
    wait_for_in_progress_runs: bool = True,
    minimum_interval_seconds: Optional[int] = None,
    description: Optional[str] = None,
    default_status: DefaultSensorStatus = DefaultSensorStatus.STOPPED,
) -> MultiAssetSensorDefinition:
    """Constructs a sensor that will monitor the parents of the provided assets and materialize an asset
    based on the materialization of its parents. This will keep the monitored assets up to date with the
    latest data available to them. The sensor defaults to materializing an asset when all of
    its parents have materialized, but it can be set to materialize an asset when any of its
    parents have materialized.

    Example:
        If you have the following asset graph:

            .. code-block:: python

                a       b       c
                \       /\      /
                    d       e
                    \       /
                        f

        and create the sensor:

            .. code-block:: python

                build_asset_reconciliation_sensor(
                        AssetSelection.assets(d, e, f),
                        name="my_reconciliation_sensor",
                        and_condition=True,
                        wait_for_in_progress_runs=True
                )

        You will observe the following behavior:
            1. If a, b, and c are all materialized, then on the next sensor tick, the sensor will see that d and e can
                be materialized. Since d and e will be materialized, f can also be materialized. The sensor will kick off a
                run that will materialize d, e, and f.
            2. If on the next sensor tick, a, b, and c have not been materialized again the sensor will not launch a run.
            3. If before the next sensor tick, just asset a and b have been materialized, the sensor will launch a run to
                materialize d.
            4. If asset c is materialized by the next sensor tick, the sensor will see that e can be materialized (since b and
                c have both been materialized since the last materialization of e). The sensor will also see that f can be materialized
                since d was updated in the previous sensor tick and e will be materialized by the sensor. The sensor will launch a run
                the materialize e and f.
            5. If by the next sensor tick, only asset b has been materialized. The sensor will not launch a run since d and e both have
                a parent that has not been updated.
            6. If during the next sensor tick, there is a materialization of a in progress, the sensor will not launch a run to
                materialize d. Once a has completed materialization, the next sensor tick will launch a run to materialize d.

    Other considerations:
        If an asset has a SourceAsset as a parent, and that source asset points to an external data source (ie the
            source asset does not point to an asset in another repository), the sensor will not know when to consider
            the source asset "materialized". If you have the asset graph:

                .. code-block:: python

                    x       source_asset
                    \       /
                        y

            and create the sensor:

                .. code-block:: python

                    build_asset_reconciliation_sensor(AssetSelection.assets(y), name="my_reconciliation_sensor")

            y will never be updated because source_asset is never considered "materialized. In this case you should create the
            sensor build_asset_reconciliation_sensor(AssetSelection.assets(y), name="my_reconciliation_sensor", and_condition=False)
            which will cause y to be materialized when x is materialized.

    Args:
        selection (AssetSelection): The group of assets you want to keep up-to-date
        name (str): The name to give the sensor.
        and_condition (bool): If True (the default) the sensor will only materialize an asset when
            all of its parents have materialized. If False, the sensor will materialize an asset when
            any of its parents have materialized.
        wait_for_in_progress_runs (bool): If True (the default), the sensor will not materialize an
            asset if there is an in-progress run that will materialize any of the asset's parents.
        minimum_interval_seconds (Optional[int]): The minimum amount of time that should elapse between sensor invocations.
        description (Optional[str]): A description for the sensor.
        default_status (DefaultSensorStatus): Whether the sensor starts as running or not. The default
            status can be overridden from Dagit or via the GraphQL API.

    Returns: A MultiAssetSensorDefinition that will monitor the parents of the provided assets to determine when
        the provided assets should be materialized
    """
    check_valid_name(name)
    return _make_sensor(
        selection=selection,
        name=name,
        and_condition=and_condition,
        wait_for_in_progress_runs=wait_for_in_progress_runs,
        minimum_interval_seconds=minimum_interval_seconds,
        description=description,
        default_status=default_status,
    )
