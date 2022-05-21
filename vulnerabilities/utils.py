# Copyright (c) nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/vulnerablecode/
# The VulnerableCode software is licensed under the Apache License version 2.0.
# Data generated with VulnerableCode require an acknowledgment.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with VulnerableCode or any VulnerableCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with VulnerableCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  VulnerableCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  VulnerableCode is a free software from nexB Inc. and others.
#  Visit https://github.com/nexB/vulnerablecode/ for support and download.

import bisect
import dataclasses
import json
import logging
import os
import re
from collections import defaultdict
from functools import total_ordering
from typing import List
from typing import Optional
from typing import Tuple
from unittest.mock import MagicMock

import requests
import saneyaml
import toml
import urllib3
from packageurl import PackageURL
from univers.version_range import RANGE_CLASS_BY_SCHEMES

logger = logging.getLogger(__name__)

cve_regex = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
is_cve = cve_regex.match
find_all_cve = cve_regex.findall


@dataclasses.dataclass(order=True, frozen=True)
class AffectedPackage:
    vulnerable_package: PackageURL
    patched_package: Optional[PackageURL] = None


def load_yaml(path):
    with open(path) as f:
        return saneyaml.load(f)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_toml(path):
    with open(path) as f:
        return toml.load(f)


def fetch_yaml(url):
    response = requests.get(url)
    return saneyaml.load(response.content)


# FIXME: Remove this entirely after complete importer-improver migration
create_etag = MagicMock()


def split_markdown_front_matter(text: str) -> Tuple[str, str]:
    """
    Return a tuple of (front matter, markdown body) strings split from a
    ``text`` string. Each can be an empty string. This is used when security
    advisories are provided in this format.
    """
    lines = text.splitlines()
    if not lines:
        return "", ""

    if lines[0] == "---":
        lines = lines[1:]
        text = "\n".join(lines)
        frontmatter, _, markdown = text.partition("\n---\n")
        return frontmatter, markdown

    return "", text


def contains_alpha(string):
    """
    Return True if the input 'string' contains any alphabet
    """

    return any([c.isalpha() for c in string])


def requests_with_5xx_retry(max_retries=5, backoff_factor=0.5):
    """
    Returns a requests sessions which retries on 5xx errors with
    a backoff_factor
    """
    retries = urllib3.Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        raise_on_status=True,
        status_forcelist=range(500, 600, 1),
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retries)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@total_ordering
class VersionedPackage:
    """
    A PackageURL with a Version class.
    This class is used to  get around bisect module's lack of supplying custom
    comparator. Get rid of this once we use python 3.10 which supports this.
    See https://github.com/python/cpython/pull/20556
    """

    def __init__(self, purl: PackageURL):
        self.purl = purl
        vrc = RANGE_CLASS_BY_SCHEMES.get(purl.type)
        self.version = vrc.version_class(purl.version)

    def __eq__(self, other):
        return self.version == other.version

    def __lt__(self, other):
        return self.version < other.version


def evolve_purl(purl, **kwargs):
    """
    Return a new PackageURL derived from the ``purl`` PackageURL where any of
    the provided kwarg replaces the corresponding attribute of this PackageURL.
    Qaulifiers if provided must be a mapping
    For example::
    >>> purl = PackageURL.from_string("pkg:generic/this@1.2.3")
    >>> evolved = PackageURL.from_string("pkg:npm/@baz/that@2.2.3?foo=bar")
    >>> evolve_purl(purl,
    ...   type="npm", namespace="@baz", name="that",
    ...   version="2.2.3", qualifiers={"foo": "bar"}
    ... ) == evolved
    True

    """
    if not kwargs:
        return PackageURL.from_string(str(purl))

    kwargs = {name: value for name, value in kwargs.items() if hasattr(purl, name)}
    merged = purl.to_dict()
    merged.update(kwargs)
    return PackageURL(**merged)


def nearest_patched_package(
    vulnerable_packages: List[PackageURL], resolved_packages: List[PackageURL]
) -> List[AffectedPackage]:
    """
    Return a list of Affected Packages for each Patched package.
    """

    vulnerable_packages = sorted([VersionedPackage(package) for package in vulnerable_packages])
    resolved_packages = sorted([VersionedPackage(package) for package in resolved_packages])

    resolved_package_count = len(resolved_packages)
    affected_package_with_patched_package_objects = []

    for vulnerable_package in vulnerable_packages:
        patched_package_index = bisect.bisect_right(resolved_packages, vulnerable_package)
        patched_package = None
        if patched_package_index < resolved_package_count:
            patched_package = resolved_packages[patched_package_index]

        affected_package_with_patched_package_objects.append(
            AffectedPackage(
                vulnerable_package=vulnerable_package.purl,
                patched_package=patched_package.purl if patched_package else None,
            )
        )

    return affected_package_with_patched_package_objects


# TODO: Replace this with combination of @classmethod and @property after upgrading to python 3.9
class classproperty(object):
    def __init__(self, fget):
        self.fget = fget

    def __get__(self, owner_self, owner_cls):
        return self.fget(owner_cls)


def get_item(object: dict, *attributes):
    """
    Return `item` by going through all the `attributes` present in the `json_object`

    Do a DFS for the `item` in the `json_object` by traversing the `attributes`
    and return None if can not traverse through the `attributes`
    For example:
    >>> get_item({'a': {'b': {'c': 'd'}}}, 'a', 'b', 'c')
    'd'
    >>> assert(get_item({'a': {'b': {'c': 'd'}}}, 'a', 'b', 'e')) == None
    """
    if not object:
        return
    item = object
    for attribute in attributes:
        if attribute not in item:
            logger.error(f"Missing attribute {attribute} in {item}")
            return None
        item = item[attribute]
    return item


class GitHubTokenError(Exception):
    pass


class GraphQLError(Exception):
    pass


def fetch_github_graphql_query(graphql_query: dict):
    """
    Return results from calling the Github graphql API with the ``graphql_query`` mapping.
    Raise a GitHubTokenError if the "GH_TOKEN" environment variable is not set.
    Raise a GraphQLError on query errors.
    """
    gh_token = os.environ.get("GH_TOKEN", None)
    # graphql api cannot work without api token
    if not gh_token:
        msg = "Cannot call GitHub API without a token set in the GH_TOKEN environment variable."
        logger.error(msg)
        raise GitHubTokenError(msg)

    response = _get_gh_response(gh_token=gh_token, graphql_query=graphql_query)

    message = response.get("message")
    if message and message == "Bad credentials":
        raise GitHubTokenError(f"Invalid GitHub token: {message}")

    errors = response.get("errors")
    if errors:
        raise GraphQLError(errors)

    return response


def _get_gh_response(gh_token, graphql_query):
    """
    Convenience function to easy mocking in tests
    """
    endpoint = "https://api.github.com/graphql"
    headers = {"Authorization": f"bearer {gh_token}"}
    return requests.post(endpoint, headers=headers, json=graphql_query).json()


def dedupe(original: List) -> List:
    """
    Remove all duplicate items and return a new list preserving ordering
    >>> dedupe(["z","i","a","a","d","d"])
    ['z', 'i', 'a', 'd']
    """
    return list(dict.fromkeys(original))


def get_affected_packages_by_patched_package(
    affected_packages: List[AffectedPackage],
):
    """
    Return a mapping of list of vulnerable purls keyed by
    purl which fix those vulnerable package.
    """
    affected_packages_by_patched_package = defaultdict(list)
    for package in affected_packages:
        affected_packages_by_patched_package[package.patched_package].append(
            package.vulnerable_package
        )
    return affected_packages_by_patched_package


# This code has been vendored from scancode.
# https://github.com/nexB/scancode-toolkit/blob/aba31126dcb3ab57f2b885090f7145f69b67351a/src/packagedcode/utils.py#L111
def build_description(summary, description):
    """
    Return a description string from a summary and description
    """
    summary = (summary or "").strip()
    description = (description or "").strip()

    if not description:
        description = summary
    else:
        if summary and summary not in description:
            description = "\n".join([summary, description])

    return description