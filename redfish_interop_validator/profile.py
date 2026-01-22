
# Copyright Notice:
# Copyright 2017-2025 DMTF. All rights reserved.
# License: BSD 3-Clause License. For full text see link:
# https://github.com/DMTF/Redfish-Service-Validator/LICENSE.md

import os
import re
import glob
import json
import logging

from urllib.request import urlopen
from collections.abc import Mapping

from redfish_interop_validator.helper import splitVersionString, versionpattern

my_logger = logging.getLogger()
my_logger.setLevel(logging.DEBUG)


def hashProfile(profile):
    from hashlib import md5
    my_md5 = md5(json.dumps(profile, sort_keys=True).encode())
    return my_md5.hexdigest()


def checkProfileAgainstSchema(profile, schema):
    """
    Checks if a profile is conformant
    """
    # what is required in a profile? use the json schema
    import jsonschema
    try:
        jsonschema.validate(profile, schema)
    except jsonschema.ValidationError as e:
        my_logger.exception(e)
        my_logger.info('ValidationError')
        return False
    except jsonschema.SchemaError as e:
        my_logger.exception(e)
        my_logger.info('SchemaError')
        return False
    # consider @odata.type, with regex
    return True


defaultrepository = 'http://redfish.dmtf.org/profiles'


def getProfilesMatchingName(name, directories):
    pattern = r'\.{}\.'.format(versionpattern)
    filepattern = re.compile(pattern.join(name.split('.')) + "|{}".format(name.replace('.', '\.')))
    for dirname in directories:
        for file in glob.glob(os.path.join(dirname, '*.json')):
            if filepattern.match(os.path.basename(file)):
                yield file

def dict_merge(dct, merge_dct):
    """
    Smart recursive dict merge

    Chooses the most restrictive values when merging:
    - MinVersion/MinCount: uses maximum (most restrictive)
    - MaxVersion/MaxCount: uses minimum (most restrictive)
    - ReadRequirement/WriteRequirement: uses hierarchy (Mandatory > Recommended > IfImplemented > Supported)
    - Arrays: merges/unions instead of replacing
    - ConditionalRequirements: merges lists

    :param dct: dict onto which the merge is executed
    :param merge_dct: dict merged into dct
    :return: None
    """
    # Requirement hierarchy: higher value = more restrictive
    REQUIREMENT_HIERARCHY = {
        'Mandatory': 4,
        'Recommended': 3,
        'IfImplemented': 2,
        'Supported': 1,
        None: 4 # per DSP0272 when unspecified Requirement, it's considered Mandatory
    }

    def get_more_restrictive_requirement(req1, req2, requirement_type='ReadRequirement'):
        """Returns the more restrictive requirement

        For ReadRequirement: returns value with hierarchy 4 (Mandatory)
        For WriteRequirement: returns value with hierarchy 0 (least restrictive)
        """
        if requirement_type == 'ReadRequirement':
            # Return the requirement with hierarchy value 4 (Mandatory)
            for req in [req1, req2]:
                if REQUIREMENT_HIERARCHY.get(req, 0) == 4:
                    return req
            return req1 if req1 is not None else req2
        else:  # WriteRequirement
            # Return the requirement with hierarchy value 0 (least restrictive)
            return min(req1, req2, key=lambda r: REQUIREMENT_HIERARCHY.get(r, 0))

    def compare_versions(ver1, ver2, use_max=True):
        """Compare two version strings and return the more restrictive one"""
        if ver1 is None:
            return ver2
        if ver2 is None:
            return ver1

        return max(ver1, ver2, key=lambda v: splitVersionString(str(v))) if use_max else min(ver1, ver2, key=lambda v: splitVersionString(str(v)))

    def merge_lists(list1, list2):
        """Merge two lists, removing duplicates while preserving order"""
        seen = set()
        merged = []
        for item in list1 + list2:
            item_key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else item
            if item_key not in seen:
                seen.add(item_key)
                merged.append(item)
        return merged

    for k, v in merge_dct.items():
        """Build merged profile based on more restrictive values"""
        if k in ('ReadRequirement', 'WriteRequirement'):
            # Handle missing keys as None (Mandatory for ReadRequirement per DSP0272)
            dct[k] = get_more_restrictive_requirement(dct.get(k), v, requirement_type=k)
        elif k not in dct:
            dct[k] = v
        elif k == 'MinVersion':
            dct[k] = compare_versions(dct[k], v, use_max=True)
        elif k == 'MaxVersion':
            dct[k] = compare_versions(dct[k], v, use_max=False)
        elif k == 'MinCount':
            dct[k] = max(dct[k], v)
        elif k == 'MaxCount':
            dct[k] = min(dct[k], v)
        elif isinstance(dct[k], dict) and isinstance(v, Mapping):
            dict_merge(dct[k], v)
        elif isinstance(dct[k], list) and isinstance(v, list):
            dct[k] = merge_lists(dct[k], v)
        elif type(dct[k]) != type(v):
            my_logger.warning(
                f'Type conflict during merge for key "{k}": '
                f'{type(dct[k]).__name__} vs {type(v).__name__}. '
                f'Keeping existing value.'
            )


def updateWithProfile(profile, data):
    dict_merge(data, profile)
    return data


def getProfileFromRepo(profilename, repo=None):
    try:
        if repo is None:
            repo = 'http://redfish.dmtf.org/profiles'

        urlpath = urlopen(repo)
        string = urlpath.read().decode('utf-8')

        pattern = r'\.{}\.'.format(versionpattern)
        filepattern = re.compile(pattern.join(profilename.split('.')))

        filelist = filepattern.findall(string)

        profilename = None
        for filename in filelist:
            filename = filename[:-1]
            if profilename is None:
                profilename = filename
                continue
            profilename = max(profilename, filename)
        if profilename is None:
            return None

        remotefile = urlopen(repo + '/' + profilename)
        return remotefile.read()
    except Exception as e:
        print(e)
        return None

# Presumes the cache does not need to handle multiple of the same profile with different versions
profile_cache = {}

def parseProfileInclude(target_name, target_profile_info, directories, online):
    # Grab data of profile from online or locally

    min_version = splitVersionString(target_profile_info.get('MinVersion', '1.0.0'))
    target_version = 'v{}_{}_{}'.format(*min_version)
    target_file = '{}.{}'.format(target_name, 'json')

    if target_file in profile_cache:
        return profile_cache[target_file]

    # get max filename
    repo = target_profile_info.get('Repository')
    if online:
        data = getProfileFromRepo(target_file, repo)
    else:
        data = None

    if data is None:
        target_list = list(getProfilesMatchingName(target_file, directories))
        if len(target_list) > 0:
            max_version = (1,0,0)
            for target_name in target_list:
                with open(target_name) as f:
                    my_profile = json.load(f)
                    my_version = splitVersionString(my_profile.get('ProfileVersion', '1.0.0'))
                    max_version = max(max_version, my_version)
                    if my_version == max_version or data is None:
                        data = my_profile
            if min_version > max_version:
                my_logger.warning('RequiredProfile Version Warning: File version smaller than target MinVersion')
        else:
            my_logger.error('RequiredProfile Import Error: Could not acquire this profile from local source or online {} {}'.format(target_name, repo))
            data = None

    profile_cache[target_file] = data
    return data


def getProfiles(profile, directories, chain=None, online=False):
    profile_includes, required_by_resource = [], []

    # Prevent cyclical imports when possible
    profile_name = profile.get('ProfileName')
    if chain is None:
        chain = []
    if profile_name in chain:
        my_logger.error('RequiredProfiles Import Error: Suspected duplicate/cyclical import error: {} {}'.format(chain, profile_name))
        return [], []
    chain.append(profile_name)

    # Gather all included profiles and merge them into the main profile
    # This processes them simultaneously to avoid polling the target machine multiple times
    required_profiles = profile.get('RequiredProfiles', {})
    for target_name, target_profile_info in required_profiles.items():
        profile_data = parseProfileInclude(target_name, target_profile_info, directories, online)

        if profile_data:
            # Merge the included profile's Resources into the main profile
            if 'Resources' not in profile:
                profile['Resources'] = {}
            included_resources = profile_data.get('Resources', {})
            for resource_type, resource_data in included_resources.items():
                if resource_type not in profile['Resources']:
                    profile['Resources'][resource_type] = {}
                dict_merge(profile['Resources'][resource_type], resource_data)

            profile_includes.append(profile_data)

            inner_includes, inner_reqs = getProfiles(profile_data, directories, chain)
            profile_includes.extend(inner_includes)
            required_by_resource.extend(inner_reqs)

    # Process all RequiredResourceProfile by modifying profiles
    profile_resources = profile.get('Resources', {})

    for resource_name, resource in profile_resources.items():
        # Modify just the resource or its UseCases.  Should not have concurrent UseCases and RequiredResourceProfile in Resource
        if 'UseCases' not in resource:
            modifying_objects = [resource]
        else:
            modifying_objects = resource['UseCases']
        # Use same format as RequiredProfiles
        for inner_object in modifying_objects:
            required_profiles = inner_object.get('RequiredResourceProfile', {})
            for target_name, target_profile_info in required_profiles.items():
                profile_data = parseProfileInclude(target_name, target_profile_info, directories, online)

                if profile_data:
                    target_resources = profile_data.get('Resources')
                    if resource_name in target_resources:
                        dict_merge(inner_object, target_resources[resource_name])
                        required_by_resource.append(profile_data)
                    else:
                        my_logger.error('RequiredProfiles Import Error: Import {} does not have Resource {}'.format(target_name, resource_name))

    return profile_includes, required_by_resource
