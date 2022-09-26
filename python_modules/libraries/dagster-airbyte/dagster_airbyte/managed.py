from typing import Any, Dict, List, Mapping, Optional, Tuple

from dagster_airbyte.resources import AirbyteResource
from dagster_airbyte.types import (
    AirbyteConnection,
    AirbyteDestination,
    AirbyteSource,
    InitializedAirbyteConnection,
    InitializedAirbyteDestination,
    InitializedAirbyteSource,
)
from dagster_airbyte.utils import is_basic_normalization_operation

from dagster._experimental.managed_stacks import (
    ManagedStackCheckResult,
    ManagedStackDiff,
    ManagedStackDiffSort,
)
from dagster._experimental.managed_stacks.utils import diff_dicts


def diff_sources(config_src: AirbyteSource, curr_src: AirbyteSource) -> ManagedStackCheckResult:
    dicts_same, diff = diff_dicts(
        config_src.source_configuration if config_src else {},
        curr_src.source_configuration if curr_src else {},
    )
    if not dicts_same:
        return ManagedStackDiff().with_nested(
            config_src.name if config_src else curr_src.name, diff
        )

    return ManagedStackDiff()


def diff_destinations(
    config_dst: AirbyteDestination, curr_dst: AirbyteDestination
) -> ManagedStackCheckResult:
    dicts_same, diff = diff_dicts(
        config_dst.destination_configuration if config_dst else {},
        curr_dst.destination_configuration if curr_dst else {},
    )
    if not dicts_same:
        return ManagedStackDiff().with_nested(
            config_dst.name if config_dst else curr_dst.name, diff
        )

    return ManagedStackDiff()


def conn_dict(conn: AirbyteConnection) -> Dict[str, Any]:
    return {
        "source": conn.source.name if conn.source else "Unknown",
        "destination": conn.destination.name if conn.destination else "Unknown",
        "normalize data": conn.normalize_data,
        "streams": {k: v.name for k, v in conn.stream_config.items()},
    }


def diff_connections(
    config_conn: AirbyteConnection, curr_conn: AirbyteConnection
) -> ManagedStackCheckResult:
    if not config_conn and curr_conn:
        return ManagedStackDiff(deletions=[f"Will delete {curr_conn.name}"])
    if not curr_conn and config_conn:
        return ManagedStackDiff(additions=[f"Will create {config_conn.name}"])

    dicts_same, diff = diff_dicts(conn_dict(config_conn), conn_dict(curr_conn))
    if not dicts_same:
        return ManagedStackDiff().with_nested(config_conn.name, diff)

    return ManagedStackDiff()


def reconcile_sources(
    res: AirbyteResource,
    config_sources: Mapping[str, AirbyteSource],
    existing_sources: Mapping[str, InitializedAirbyteSource],
    workspace_id: str,
    dry_run: bool,
) -> Tuple[Mapping[str, InitializedAirbyteSource], ManagedStackCheckResult]:

    diff = ManagedStackDiff(sort=ManagedStackDiffSort.BY_KEY)

    initialized_sources = {}
    for source_name in set(config_sources.keys()).union(existing_sources.keys()):
        configured_source = config_sources.get(source_name)
        existing_source = existing_sources.get(source_name)

        diff = diff.join(
            diff_sources(configured_source, existing_source.source if existing_source else None)
        )

        if not configured_source or (
            existing_source and configured_source.must_be_recreated(existing_source.source)
        ):
            initialized_sources[source_name] = existing_source
            if not dry_run:
                res.make_request(
                    endpoint="/sources/delete",
                    data={"sourceId": existing_source.source_id},
                )
            existing_source = None

        if configured_source:
            defn_id = res.get_source_definition_by_name(configured_source.source_type, workspace_id)
            base_source_defn_dict = {
                "name": configured_source.name,
                "connectionConfiguration": configured_source.source_configuration,
            }
            source_id = "TBD"
            if existing_source:
                source_id = existing_source.source_id
                if not dry_run:
                    res.make_request(
                        endpoint="/sources/update",
                        data={"sourceId": source_id, **base_source_defn_dict},
                    )
            else:
                if not dry_run:
                    create_result = res.make_request(
                        endpoint="/sources/create",
                        data={
                            "sourceDefinitionId": defn_id,
                            "workspaceId": workspace_id,
                            **base_source_defn_dict,
                        },
                    )
                    source_id = create_result["sourceId"]

            initialized_sources[source_name] = InitializedAirbyteSource(
                source=configured_source,
                source_id=source_id,
                source_definition_id=defn_id,
            )

    return initialized_sources, diff


def reconcile_destinations(
    res: AirbyteResource,
    config_destinations: Mapping[str, AirbyteDestination],
    existing_destinations: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
) -> Tuple[Mapping[str, InitializedAirbyteDestination], ManagedStackCheckResult]:

    diff = ManagedStackDiff(sort=ManagedStackDiffSort.BY_KEY)

    initialized_destinations = {}
    for destination_name in set(config_destinations.keys()).union(existing_destinations.keys()):
        configured_destination = config_destinations.get(destination_name)
        existing_destination = existing_destinations.get(destination_name)

        diff = diff.join(
            diff_destinations(
                configured_destination,
                existing_destination.destination if existing_destination else None,
            )
        )

        if not configured_destination:
            initialized_destinations[destination_name] = existing_destination
            if not dry_run:
                res.make_request(
                    endpoint="/destinations/delete",
                    data={"destinationId": existing_destination.destination_id},
                )
        else:
            defn_id = res.get_destination_definition_by_name(
                configured_destination.destination_type, workspace_id
            )
            base_destination_defn_dict = {
                "name": configured_destination.name,
                "connectionConfiguration": configured_destination.destination_configuration,
            }
            destination_id = "TBD"
            if existing_destination:
                destination_id = existing_destination.destination_id
                if not dry_run:
                    res.make_request(
                        endpoint="/destinations/update",
                        data={"destinationId": destination_id, **base_destination_defn_dict},
                    )
            else:
                if not dry_run:
                    create_result = res.make_request(
                        endpoint="/destinations/create",
                        data={
                            "destinationDefinitionId": defn_id,
                            "workspaceId": workspace_id,
                            **base_destination_defn_dict,
                        },
                    )
                    destination_id = create_result["destinationId"]

            initialized_destinations[destination_name] = InitializedAirbyteDestination(
                destination=configured_destination,
                destination_id=destination_id,
                destination_definition_id=defn_id,
            )

    return initialized_destinations, diff


def reconcile_config(
    res: AirbyteResource, objects: List[AirbyteConnection], dry_run: bool = False
) -> ManagedStackCheckResult:
    res.clear_request_cache()

    config_connections = {conn.name: conn for conn in objects}
    config_sources = {conn.source.name: conn.source for conn in objects}
    config_dests = {conn.destination.name: conn.destination for conn in objects}

    workspace_id = res.get_default_workspace()

    existing_sources: Dict[str, InitializedAirbyteSource] = {
        source_json["name"]: InitializedAirbyteSource.from_api_json(source_json)
        for source_json in res.make_request(
            endpoint="/sources/list", data={"workspaceId": workspace_id}
        ).get("sources", [])
    }
    existing_dests: Dict[str, InitializedAirbyteDestination] = {
        destination_json["name"]: InitializedAirbyteDestination.from_api_json(destination_json)
        for destination_json in res.make_request(
            endpoint="/destinations/list", data={"workspaceId": workspace_id}
        ).get("destinations", [])
    }

    connections_diff = reconcile_connections_pre(
        res, config_connections, existing_sources, existing_dests, workspace_id, dry_run
    )

    all_sources, sources_diff = reconcile_sources(
        res, config_sources, existing_sources, workspace_id, dry_run
    )
    all_dests, dests_diff = reconcile_destinations(
        res, config_dests, existing_dests, workspace_id, dry_run
    )

    reconcile_connections_post(
        res,
        config_connections,
        all_sources,
        all_dests,
        workspace_id,
        dry_run,
    )

    return (
        ManagedStackDiff(sort=ManagedStackDiffSort.BY_KEY)
        .with_nested("Sources", sources_diff)
        .with_nested("Destinations", dests_diff)
        .with_nested("Connections", connections_diff)
    )


def reconcile_normalization(
    res: AirbyteResource,
    existing_connection_id: Optional[str],
    destination: InitializedAirbyteDestination,
    normalization_config: Optional[bool],
    workspace_id: str,
) -> Optional[str]:
    existing_basic_norm_op_id = None
    if existing_connection_id:
        operations = res.make_request(
            endpoint="/operations/list",
            data={"connectionId": existing_connection_id},
        )["operations"]
        existing_basic_norm_op_id = next(
            (operation for operation in operations if is_basic_normalization_operation(operation)),
            None,
        )["operationId"]

    if normalization_config is not False:
        if res.does_dest_support_normalization(destination, workspace_id):
            if existing_basic_norm_op_id:
                return existing_basic_norm_op_id
            else:
                return res.make_request(
                    endpoint="/operations/create",
                    data={
                        "workspaceId": workspace_id,
                        "name": "Normalization",
                        "operatorConfiguration": {
                            "operatorType": "normalization",
                            "normalization": {"option": "basic"},
                        },
                    },
                )["operationId"]
        elif normalization_config is True:
            raise Exception(
                f"Destination {destination.destination.name} does not support normalization."
            )

    return None


def reconcile_connections_pre(
    res: AirbyteResource,
    config_connections: Mapping[str, AirbyteConnection],
    existing_sources: Mapping[str, InitializedAirbyteSource],
    existing_destinations: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
) -> ManagedStackCheckResult:
    diff = ManagedStackDiff(sort=ManagedStackDiffSort.BY_KEY)

    existing_connections = {
        connection_json["name"]: InitializedAirbyteConnection.from_api_json(
            connection_json, existing_sources, existing_destinations
        )
        for connection_json in res.make_request(
            endpoint="/connections/list", data={"workspaceId": workspace_id}
        ).get("connections", [])
    }

    for conn_name in set(config_connections.keys()).union(existing_connections.keys()):
        config_conn = config_connections.get(conn_name)
        existing_conn = existing_connections.get(conn_name)

        diff = diff.join(
            diff_connections(config_conn, existing_conn.connection if existing_conn else None)
        )

        if existing_conn and (
            not config_conn or config_conn.must_be_recreated(existing_conn.connection)
        ):
            if not dry_run:
                res.make_request(
                    endpoint="/connections/delete",
                    data={"connectionId": existing_conn.connection_id},
                )

    return diff


def reconcile_connections_post(
    res: AirbyteResource,
    config_connections: Mapping[str, AirbyteConnection],
    init_sources: Mapping[str, InitializedAirbyteSource],
    init_dests: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
) -> None:

    existing_connections = {
        connection_json["name"]: InitializedAirbyteConnection.from_api_json(
            connection_json, init_sources, init_dests
        )
        for connection_json in res.make_request(
            endpoint="/connections/list", data={"workspaceId": workspace_id}
        ).get("connections", [])
    }

    for conn_name, config_conn in config_connections.items():
        existing_conn = existing_connections.get(conn_name)

        normalization_operation_id = None
        if not dry_run:
            destination = init_dests[config_conn.destination.name]

            # Enable or disable basic normalization based on config
            normalization_operation_id = reconcile_normalization(
                res,
                existing_connections.get("name", {}).get("connectionId"),
                destination,
                config_conn.normalize_data,
                workspace_id,
            )

        configured_streams = []
        if not dry_run:
            source = init_sources[config_conn.source.name]
            schema = res.get_source_schema(source)
            base_streams = schema["catalog"]["streams"]

            configured_streams = [
                res._customize_stream(stream, config_conn.stream_config)
                for stream in base_streams
                if stream["stream"]["name"] in config_conn.stream_config
            ]

        connection_base_json = {
            "name": conn_name,
            "namespaceDefinition": "source",
            "namespaceFormat": "${SOURCE_NAMESPACE}",
            "prefix": "",
            "operationIds": [normalization_operation_id] if normalization_operation_id else [],
            "syncCatalog": {"streams": configured_streams},
            "scheduleType": "manual",
            "status": "active",
        }

        if existing_conn:
            if not dry_run:
                source = init_sources[conn_name]
                res.make_request(
                    endpoint="/connections/update",
                    data={
                        **connection_base_json,
                        "sourceCatalogId": res.get_source_catalog_id(source.source_id),
                        "connectionId": existing_conn.connection_id,
                    },
                )
        else:
            if not dry_run:
                source = init_sources[config_conn.source.name]
                destination = init_dests[config_conn.destination.name]
                res.make_request(
                    endpoint="/connections/create",
                    data={
                        **connection_base_json,
                        "sourceCatalogId": res.get_source_catalog_id(source.source_id),
                        "sourceId": source.source_id,
                        "destinationId": destination.destination_id,
                    },
                )


def reconcile_connections(
    res: AirbyteResource,
    connections: List[AirbyteConnection],
    init_sources: Mapping[str, InitializedAirbyteSource],
    init_dests: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
) -> ManagedStackCheckResult:
    diff = ManagedStackDiff(sort=ManagedStackDiffSort.BY_KEY)

    existing_connections = {
        connection["name"]: connection
        for connection in res.make_request(
            endpoint="/connections/list", data={"workspaceId": workspace_id}
        ).get("connections", [])
    }

    config_connection_names = {conn.name for conn in connections}
    connections_to_delete = {
        name: connection["connectionId"]
        for name, connection in existing_connections.items()
        if name not in config_connection_names
    }

    for connection in connections:
        name = connection.name

        normalization_operation_id = None
        if not dry_run:
            destination = init_dests[connection.destination.name]

            # Enable or disable basic normalization based on config
            normalization_operation_id = reconcile_normalization(
                res,
                existing_connections.get("name", {}).get("connectionId"),
                destination,
                connection.normalize_data,
                workspace_id,
            )

        configured_streams = []
        if not dry_run:
            source = init_sources[connection.source.name]
            schema = res.get_source_schema(source)
            base_streams = schema["catalog"]["streams"]

            configured_streams = [
                res._customize_stream(stream, connection.stream_config)
                for stream in base_streams
                if stream["stream"]["name"] in connection.stream_config
            ]

        connection_base_json = {
            "name": name,
            "namespaceDefinition": "source",
            "namespaceFormat": "${SOURCE_NAMESPACE}",
            "prefix": "",
            "operationIds": [normalization_operation_id] if normalization_operation_id else [],
            "syncCatalog": {"streams": configured_streams},
            "scheduleType": "manual",
            "status": "active",
        }

        if name in existing_connections:
            connection_id = existing_connections[name]["connectionId"]
            existing_connection_data = existing_connections[name]
            existing_connection = AirbyteConnection.from_api_json(
                existing_connection_data, init_sources, init_dests
            )
            diff = diff.join(diff_connections(connection, existing_connection))

            if not dry_run:
                source = init_sources[connection.source.name]
                res.make_request(
                    endpoint="/connections/update",
                    data={
                        **connection_base_json,
                        "sourceCatalogId": res.get_source_catalog_id(source.source_id),
                        "connectionId": connection_id,
                    },
                )
        else:
            diff = diff.join(diff_connections(connection, None))
            if not dry_run:
                source = init_sources[connection.source.name]
                destination = init_dests[connection.destination.name]
                res.make_request(
                    endpoint="/connections/create",
                    data={
                        **connection_base_json,
                        "sourceCatalogId": res.get_source_catalog_id(source.source_id),
                        "sourceId": source.source_id,
                        "destinationId": destination.destination_id,
                    },
                )

    for connection_id in connections_to_delete.values():
        if not dry_run:
            res.make_request(endpoint="/connections/delete", data={"connectionId": connection_id})
    return diff
