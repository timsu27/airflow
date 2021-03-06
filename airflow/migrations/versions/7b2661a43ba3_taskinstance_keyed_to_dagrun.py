#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""TaskInstance keyed to DagRun

Revision ID: 7b2661a43ba3
Revises: 142555e44c17
Create Date: 2021-07-15 15:26:12.710749

"""

from collections import defaultdict

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import and_, column, select, table

from airflow.models.base import COLLATION_ARGS

ID_LEN = 250

# revision identifiers, used by Alembic.
revision = '7b2661a43ba3'
down_revision = '142555e44c17'
branch_labels = None
depends_on = None


def _mssql_datetime():
    from sqlalchemy.dialects import mssql

    return mssql.DATETIME2(precision=6)


# Just Enough Table to run the conditions for update.
task_instance = table(
    'task_instance',
    column('task_id', sa.String),
    column('dag_id', sa.String),
    column('run_id', sa.String),
    column('execution_date', sa.TIMESTAMP),
)
task_reschedule = table(
    'task_reschedule',
    column('task_id', sa.String),
    column('dag_id', sa.String),
    column('run_id', sa.String),
    column('execution_date', sa.TIMESTAMP),
)
dag_run = table(
    'dag_run',
    column('dag_id', sa.String),
    column('run_id', sa.String),
    column('execution_date', sa.TIMESTAMP),
)


def get_table_constraints(conn, table_name):
    """
    This function return primary and unique constraint
    along with column name. Some tables like `task_instance`
    is missing the primary key constraint name and the name is
    auto-generated by the SQL server. so this function helps to
    retrieve any primary or unique constraint name.
    :param conn: sql connection object
    :param table_name: table name
    :return: a dictionary of ((constraint name, constraint type), column name) of table
    :rtype: defaultdict(list)
    """
    query = """SELECT tc.CONSTRAINT_NAME , tc.CONSTRAINT_TYPE, ccu.COLUMN_NAME
     FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
     JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE AS ccu ON ccu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
     WHERE tc.TABLE_NAME = '{table_name}' AND
     (tc.CONSTRAINT_TYPE = 'PRIMARY KEY' or UPPER(tc.CONSTRAINT_TYPE) = 'UNIQUE')
    """.format(
        table_name=table_name
    )
    result = conn.execute(query).fetchall()
    constraint_dict = defaultdict(lambda: defaultdict(list))
    for constraint, constraint_type, col_name in result:
        constraint_dict[constraint_type][constraint].append(col_name)
    return constraint_dict


def upgrade():
    """Apply TaskInstance keyed to DagRun"""
    conn = op.get_bind()
    dialect_name = conn.dialect.name

    run_id_col_type = sa.String(length=ID_LEN, **COLLATION_ARGS)

    if dialect_name == 'sqlite':
        naming_convention = {
            "uq": "%(table_name)s_%(column_0_N_name)s_key",
        }
        with op.batch_alter_table('dag_run', naming_convention=naming_convention, recreate="always"):
            # The naming_convention force the previously un-named UNIQUE constraints to have the right name --
            # but we still need to enter the context manager to trigger it
            pass
    elif dialect_name == 'mysql':
        with op.batch_alter_table('dag_run') as batch_op:
            batch_op.alter_column('dag_id', existing_type=sa.String(length=ID_LEN), type_=run_id_col_type)
            batch_op.alter_column('run_id', existing_type=sa.String(length=ID_LEN), type_=run_id_col_type)
            batch_op.drop_constraint('dag_id', 'unique')
            batch_op.drop_constraint('dag_id_2', 'unique')
            batch_op.create_unique_constraint(
                'dag_run_dag_id_execution_date_key', ['dag_id', 'execution_date']
            )
            batch_op.create_unique_constraint('dag_run_dag_id_run_id_key', ['dag_id', 'run_id'])
    elif dialect_name == 'mssql':

        # _Somehow_ mssql was missing these constraints entirely!
        with op.batch_alter_table('dag_run') as batch_op:
            batch_op.create_unique_constraint(
                'dag_run_dag_id_execution_date_key', ['dag_id', 'execution_date']
            )
            batch_op.create_unique_constraint('dag_run_dag_id_run_id_key', ['dag_id', 'run_id'])

    # First create column nullable
    op.add_column('task_instance', sa.Column('run_id', type_=run_id_col_type, nullable=True))
    op.add_column('task_reschedule', sa.Column('run_id', type_=run_id_col_type, nullable=True))

    # Then update the new column by selecting the right value from DagRun
    update_query = _multi_table_update(dialect_name, task_instance, task_instance.c.run_id)
    op.execute(update_query)

    #
    # TaskReschedule has a FK to TaskInstance, so we have to update that before
    # we can drop the TI.execution_date column

    update_query = _multi_table_update(dialect_name, task_reschedule, task_reschedule.c.run_id)
    op.execute(update_query)

    with op.batch_alter_table('task_reschedule', schema=None) as batch_op:
        batch_op.alter_column('run_id', existing_type=run_id_col_type, existing_nullable=True, nullable=False)

        batch_op.drop_constraint('task_reschedule_dag_task_date_fkey', 'foreignkey')
        if dialect_name == "mysql":
            # Mysql creates an index and a constraint -- we have to drop both
            batch_op.drop_index('task_reschedule_dag_task_date_fkey')
        batch_op.drop_index('idx_task_reschedule_dag_task_date')

    with op.batch_alter_table('task_instance', schema=None) as batch_op:
        # Then make it non-nullable
        batch_op.alter_column('run_id', existing_type=run_id_col_type, existing_nullable=True, nullable=False)

        # TODO: Is this right for non-postgres?
        if dialect_name == 'mssql':
            constraints = get_table_constraints(conn, "task_instance")
            pk, _ = constraints['PRIMARY KEY'].popitem()
            batch_op.drop_constraint(pk, type_='primary')
        elif dialect_name not in ('sqlite'):
            batch_op.drop_constraint('task_instance_pkey', type_='primary')
        batch_op.create_primary_key('task_instance_pkey', ['dag_id', 'task_id', 'run_id'])

        batch_op.drop_index('ti_dag_date')
        batch_op.drop_index('ti_state_lkp')
        batch_op.drop_column('execution_date')
        batch_op.create_foreign_key(
            'task_instance_dag_run_fkey',
            'dag_run',
            ['dag_id', 'run_id'],
            ['dag_id', 'run_id'],
            ondelete='CASCADE',
        )

        batch_op.create_index('ti_dag_run', ['dag_id', 'run_id'])
        batch_op.create_index('ti_state_lkp', ['dag_id', 'task_id', 'run_id', 'state'])

    with op.batch_alter_table('task_reschedule', schema=None) as batch_op:
        batch_op.drop_column('execution_date')
        batch_op.create_index(
            'idx_task_reschedule_dag_task_run',
            ['dag_id', 'task_id', 'run_id'],
            unique=False,
        )
        # _Now_ there is a unique constraint on the columns in TI we can re-create the FK from TaskReschedule
        batch_op.create_foreign_key(
            'task_reschedule_ti_fkey',
            'task_instance',
            ['dag_id', 'task_id', 'run_id'],
            ['dag_id', 'task_id', 'run_id'],
            ondelete='CASCADE',
        )

        # https://docs.microsoft.com/en-us/sql/relational-databases/errors-events/mssqlserver-1785-database-engine-error?view=sql-server-ver15
        ondelete = 'CASCADE' if dialect_name != 'mssql' else 'NO ACTION'
        batch_op.create_foreign_key(
            'task_reschedule_dr_fkey',
            'dag_run',
            ['dag_id', 'run_id'],
            ['dag_id', 'run_id'],
            ondelete=ondelete,
        )


def downgrade():
    """Unapply TaskInstance keyed to DagRun"""
    dialect_name = op.get_bind().dialect.name

    if dialect_name == "mssql":
        col_type = _mssql_datetime()
    else:
        col_type = sa.TIMESTAMP(timezone=True)

    op.add_column('task_instance', sa.Column('execution_date', col_type, nullable=True))
    op.add_column('task_reschedule', sa.Column('execution_date', col_type, nullable=True))

    update_query = _multi_table_update(dialect_name, task_instance, task_instance.c.execution_date)
    op.execute(update_query)

    update_query = _multi_table_update(dialect_name, task_reschedule, task_reschedule.c.execution_date)
    op.execute(update_query)

    with op.batch_alter_table('task_reschedule', schema=None) as batch_op:
        batch_op.alter_column(
            'execution_date', existing_type=col_type, existing_nullable=True, nullable=False
        )

        # Can't drop PK index while there is a FK referencing it
        batch_op.drop_constraint('task_reschedule_ti_fkey')
        batch_op.drop_constraint('task_reschedule_dr_fkey')
        batch_op.drop_index('idx_task_reschedule_dag_task_run')

    with op.batch_alter_table('task_instance', schema=None) as batch_op:
        batch_op.alter_column(
            'execution_date', existing_type=col_type, existing_nullable=True, nullable=False
        )

        batch_op.drop_constraint('task_instance_pkey', type_='primary')
        batch_op.create_primary_key('task_instance_pkey', ['dag_id', 'task_id', 'execution_date'])

        batch_op.drop_constraint('task_instance_dag_run_fkey', type_='foreignkey')
        batch_op.drop_index('ti_dag_run')
        batch_op.drop_index('ti_state_lkp')
        batch_op.create_index('ti_state_lkp', ['dag_id', 'task_id', 'execution_date', 'state'])
        batch_op.create_index('ti_dag_date', ['dag_id', 'execution_date'], unique=False)

        batch_op.drop_column('run_id')

    with op.batch_alter_table('task_reschedule', schema=None) as batch_op:
        batch_op.drop_column('run_id')
        batch_op.create_index(
            'idx_task_reschedule_dag_task_date',
            ['dag_id', 'task_id', 'execution_date'],
            unique=False,
        )
        # Can only create FK once there is an index on these columns
        batch_op.create_foreign_key(
            'task_reschedule_dag_task_date_fkey',
            'task_instance',
            ['dag_id', 'task_id', 'execution_date'],
            ['dag_id', 'task_id', 'execution_date'],
            ondelete='CASCADE',
        )


def _multi_table_update(dialect_name, target, column):
    condition = dag_run.c.dag_id == target.c.dag_id
    if column == target.c.run_id:
        condition = and_(condition, dag_run.c.execution_date == target.c.execution_date)
    else:
        condition = and_(condition, dag_run.c.run_id == target.c.run_id)

    if dialect_name == "sqlite":
        # Most SQLite versions don't support multi table update (and SQLA doesn't know about it anyway), so we
        # need to do a Correlated subquery update
        sub_q = select([dag_run.c[column.name]]).where(condition)

        return target.update().values({column: sub_q})
    else:
        return target.update().where(condition).values({column: dag_run.c[column.name]})
