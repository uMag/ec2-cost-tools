from __future__ import unicode_literals
import re
import json
import collections

import requests

__version__ = '0.1.0'

LINUX_ON_DEMAND_PRICE_URL = (
    'http://a0.awsstatic.com/pricing/1/ec2/linux-od.min.js'
)
LINUX_ON_DEMAND_PREVIOUS_GEN_PRICE_URL = (
    'http://a0.awsstatic.com/pricing/1/ec2/previous-generation/linux-od.min.js'
)


def get_price_table(url):
    """Get and return price table

    """
    resp = requests.get(url)
    content = resp.content
    callback_prefix = 'callback('
    callback_suffix = ');'
    prefix_index = content.find(callback_prefix) + len(callback_prefix)
    suffix_index = content.rfind(callback_suffix)
    content = content[prefix_index:suffix_index]
    # do a little regular expression hack to quote key name to make the
    # cotent becomes JSON format
    content = re.sub(r'(\w+?):', r'"\1":', content)
    return json.loads(content)


def price_table_to_price_mapping(table):
    """Convert price table to a dict mapping from region to instance type
    to instance info

    """
    region_price_mapping = {}
    for region_table in table['config']['regions']:
        types = {}
        for type_category in region_table['instanceTypes']:
            for size in type_category['sizes']:
                types[size['size']] = size
        region_price_mapping[region_table['region']] = types
    return region_price_mapping


def get_reserved_groups(conns):
    """Get reserved instance groups, return a dict mapping from

        (instance type, VPC or not, Availability zone, Tenancy)

    to

        [reserved instance] * instance_count

    """
    reserved_groups = collections.defaultdict(list)
    for conn in conns:
        for reserved in conn.get_all_reserved_instances():
            if reserved.state != 'active':
                continue
            key = (
                reserved.instance_type,
                'VPC' in reserved.description,
                reserved.availability_zone,
                reserved.instance_tenancy,
            )
            for _ in range(reserved.instance_count):
                reserved_groups[key].append((conn.profile_name,reserved))
    return reserved_groups


def get_instance_groups(conns):
    """Get instance groups, return a dict mapping from

        (instance type, VPC ID, Availability zone, Tenancy)

    to

        list of instances

    """
    instance_groups = collections.defaultdict(list)
    for conn in conns:
        for instance in conn.get_only_instances():
            if instance.state != 'running':
                continue
            if isinstance(instance.spot_instance_request_id, basestring):
                continue
            key = (
                instance.instance_type,
		instance.vpc_id is not None,
		instance.placement,
                instance.placement_tenancy,
            )
            instance_groups[key].append((conn.profile_name,instance.vpc_id,instance))

    sorted_groups = sorted(
        instance_groups.iteritems(),
        key=lambda item: (item[0][0], len(item[1])),
        reverse=True,
    )
    return sorted_groups


def _match_reserved_instances(reserved_groups, itype, in_vpc, zone, tenancy):
    """Try to match reserved instance in reserved_groups, if it does,
    remove the instance from reserved_groups and return the reserved instance.
    If no reserved instance matches, return None

    """
    for key in [
        # match the same VPC instances first
        (itype, in_vpc, zone, tenancy),
        # since VPC doesn't really affect the billing, so we also try to
        # match the oppsite VPC setting instances too
        (itype, not in_vpc, zone, tenancy),
    ]:
        reserved_instances = reserved_groups.get(tuple(key))
        if reserved_instances:
            ri = reserved_instances.pop()
            return ri


def get_reserved_analysis(conns):
    all_reserved_groups = get_reserved_groups(conns)
    # duplicate the reserved_groups collection so we can destructively modify it when finding matches
    reserved_groups = get_reserved_groups(conns)
    instance_groups = get_instance_groups(conns)
    instance_items = []
    for (itype, is_in_vpc, zone, tenancy), values in instance_groups:
        instances = []
        for (account, vpc_id, instance) in values:
            matched = _match_reserved_instances(
                reserved_groups=reserved_groups,
                itype=itype,
                in_vpc=is_in_vpc,
                zone=zone,
                tenancy=tenancy,
            )
            covered_price = None
            if matched and matched[1].recurring_charges:
                covered_price = matched[1].recurring_charges[0].amount
            else:
                covered_price = 0
            instances.append((account, vpc_id, instance.id, covered_price, instance.tags.get('Name')))
        instance_items.append((
            (itype, zone, tenancy),
            instances,
        ))

    return dict(
        instance_items=instance_items,
        not_used_reserved_instances=reserved_groups,
        all_reserved_groups=all_reserved_groups,
    )
