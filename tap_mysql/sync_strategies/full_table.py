#!/usr/bin/env python3
# pylint: disable=duplicate-code

import copy
import singer

import tap_mysql.sync_strategies.common as common

LOGGER = singer.get_logger()

BOOKMARK_KEYS = {'last_pk_fetched', 'max_pk_values', 'version', 'initial_full_table_complete'}


def pks_are_auto_incrementing(connection, catalog_entry):
    database_name = common.get_database_name(catalog_entry)
    key_properties = common.get_key_properties(catalog_entry)

    if not key_properties:
        return False

    sql = """SELECT 1
               FROM information_schema.columns
              WHERE table_schema = '{}'
                AND table_name = '{}'
                AND column_name = '{}'

    """

    with connection.cursor() as cur:
        for pk in key_properties:
            cur.execute(sql.format(database_name,
                                   catalog_entry.table,
                                   pk))

            result = cur.fetchone()

            if not result:
                return False

    return True


def get_max_pk_values(connection, catalog_entry):
    database_name = common.get_database_name(catalog_entry)
    escaped_db = common.escape(database_name)
    escaped_table = common.escape(catalog_entry.table)

    key_properties = common.get_key_properties(catalog_entry)
    escaped_columns = [common.escape(c) for c in key_properties]

    sql = """SELECT {}
               FROM {}.{}
              ORDER BY {}
              LIMIT 1
    """

    select_column_clause = ", ".join(escaped_columns)
    order_column_clause = ", ".join([pk + " DESC" for pk in escaped_columns])

    with connection.cursor() as cur:
        cur.execute(sql.format(select_column_clause,
                               escaped_db,
                               escaped_table,
                               order_column_clause))
        result = cur.fetchone()

        return dict(zip(key_properties, result))


def generate_pk_clause(catalog_entry, state):
    key_properties = common.get_key_properties(catalog_entry)
    escaped_columns = [common.escape(c) for c in key_properties]

    where_clause = " AND ".join([pk + " > `{}`" for pk in escaped_columns])
    order_by_clause = ", ".join(['`{}`, ' for pk in escaped_columns])

    max_pk_values = singer.get_bookmark(state,
                                        catalog_entry.tap_stream_id,
                                        'max_pk_values')

    pk_comparisons = ["{} < {}".format(common.escape(pk), max_pk_values[pk])
                      for pk in key_properties]

    last_pk_fetched = singer.get_bookmark(state,
                                          catalog_entry.tap_stream_id,
                                          'last_pk_fetched')

    if last_pk_fetched:
        last_pk_values = ""
    else:
        sql = " WHERE {} ORDER BY {} ASC".format(" AND ".join(pk_comparisons),
                                                 ", ".join(escaped_columns))
    return sql



def sync_table(connection, catalog_entry, state, columns, stream_version):
    common.whitelist_bookmark_keys(BOOKMARK_KEYS, catalog_entry.tap_stream_id, state)

    bookmark = state.get('bookmarks', {}).get(catalog_entry.tap_stream_id, {})
    version_exists = True if 'version' in bookmark else False

    initial_full_table_complete = singer.get_bookmark(state,
                                                      catalog_entry.tap_stream_id,
                                                      'initial_full_table_complete')

    state_version = singer.get_bookmark(state,
                                        catalog_entry.tap_stream_id,
                                        'version')

    activate_version_message = singer.ActivateVersionMessage(
        stream=catalog_entry.stream,
        version=stream_version
    )

    # For the initial replication, emit an ACTIVATE_VERSION message
    # at the beginning so the records show up right away.
    if not initial_full_table_complete and not (version_exists and state_version is None):
        singer.write_message(activate_version_message)

    with connection.cursor() as cursor:
        key_props_are_auto_incrementing = pks_are_auto_incrementing(connection, catalog_entry)

        select_sql = common.generate_select_sql(catalog_entry, columns)

        if key_props_are_auto_incrementing:
            LOGGER.info("Detected auto-incrementing primary key(s) - will replicate incrementally")
            max_pk_values = singer.get_bookmark(state,
                                                catalog_entry.tap_stream_id,
                                                'max_pk_values')

            if not max_pk_values:
                max_pk_values = get_max_pk_values(connection, catalog_entry)

                state = singer.write_bookmark(state,
                                              catalog_entry.tap_stream_id,
                                              'max_pk_values',
                                              max_pk_values)

            pk_clause = generate_pk_clause(catalog_entry, state)

            select_sql += pk_clause

        params = {}

        common.sync_query(cursor,
                          catalog_entry,
                          state,
                          select_sql,
                          columns,
                          stream_version,
                          params)

    singer.write_message(activate_version_message)
