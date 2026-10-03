-- The version rule on accounts (specification 008 section 2, criterion 3): an observation that
-- moves only the balance opens no version, and one that changes any other contract column opens
-- one. Re-derived from bronze, comparing every column `sl_accounts` carries, and returns each
-- observation that disagrees with the model.
{%- set carried = relation_columns(
    ref('sl_accounts'),
    skip=['account_id', 'created_at', 'updated_at', '_batch_id', '_valid_from', '_valid_to',
          '_is_current']
) %}

with observed as (
    select distinct on (account_id, updated_at) *
    from {{ ref('br_corebank__accounts') }}
    order by account_id, updated_at, _batch_id
),

previous as (
    select
        *,
        lag(is_deleted) over w as previous_is_deleted,
        lag(current_balance_amount) over w as previous_balance,
        {%- for column in carried %}
            lag("{{ column }}") over w as "previous_{{ column }}",
        {%- endfor %}
        row_number() over w as observation
    from observed
    window w as (partition by account_id order by updated_at)
),

compared as (
    select
        account_id,
        updated_at,
        observation,
        is_deleted,
        previous_is_deleted,
        (current_balance_amount is distinct from previous_balance) as balance_moved,
        (
            false
            {%- for column in carried %}
                or "{{ column }}" is distinct from "previous_{{ column }}"
            {%- endfor %}
        ) as attributes_changed
    from previous
),

judged as (
    select
        c.account_id,
        c.updated_at,
        c.attributes_changed,
        c.balance_moved,
        v.account_id is not null as opened_a_version
    from compared as c
    left join {{ ref('sl_accounts') }} as v
        on c.account_id = v.account_id and c.updated_at = v._valid_from
    where
        c.observation > 1
        and not c.is_deleted
        and not c.previous_is_deleted
)

select *
from judged
where
    (attributes_changed and not opened_a_version)
    or (not attributes_changed and balance_moved and opened_a_version)
