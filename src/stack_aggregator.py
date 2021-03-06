"""An implementation of stack aggregator.

Gathers component data from the graph database and aggregate the data to be presented
by stack-analyses endpoint

Output: TBD

"""
from f8a_worker.models import WorkerResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert
import json
import datetime
from flask import current_app

import requests

from utils import (get_session_retry, select_latest_version, LICENSE_SCORING_URL_REST,
                   GREMLIN_SERVER_URL_REST, Postgres)

session = Postgres().session


def extract_component_details(component):
    """Extract details from given component."""
    github_details = {
        "dependent_projects":
            component.get("package", {}).get("libio_dependents_projects", [-1])[0],
        "dependent_repos": component.get("package", {}).get("libio_dependents_repos", [-1])[0],
        "total_releases": component.get("package", {}).get("libio_total_releases", [-1])[0],
        "latest_release_duration":
            str(datetime.datetime.fromtimestamp(component.get("package", {}).get(
                "libio_latest_release", [1496302486.0])[0])),
        "first_release_date": "Apr 16, 2010",
        "issues": {
            "month": {
                "opened": component.get("package", {}).get("gh_issues_last_month_opened", [-1])[0],
                "closed": component.get("package", {}).get("gh_issues_last_month_closed", [-1])[0]
            }, "year": {
                "opened": component.get("package", {}).get("gh_issues_last_year_opened", [-1])[0],
                "closed": component.get("package", {}).get("gh_issues_last_year_closed", [-1])[0]
            }},
        "pull_requests": {
            "month": {
                "opened": component.get("package", {}).get("gh_prs_last_month_opened", [-1])[0],
                "closed": component.get("package", {}).get("gh_prs_last_month_closed", [-1])[0]
            }, "year": {
                "opened": component.get("package", {}).get("gh_prs_last_year_opened", [-1])[0],
                "closed": component.get("package", {}).get("gh_prs_last_year_closed", [-1])[0]
            }},
        "stargazers_count": component.get("package", {}).get("gh_stargazers", [-1])[0],
        "forks_count": component.get("package", {}).get("gh_forks", [-1])[0],
        "open_issues_count": component.get("package", {}).get("gh_open_issues_count", [-1])[0],
        "contributors": component.get("package", {}).get("gh_contributors_count", [-1])[0],
        "size": "N/A"
    }
    used_by = component.get("package", {}).get("libio_usedby", [])
    used_by_list = []
    for epvs in used_by:
        slc = epvs.split(':')
        used_by_dict = {
            'name': slc[0],
            'stars': int(slc[1])
        }
        used_by_list.append(used_by_dict)
    github_details['used_by'] = used_by_list

    code_metrics = {
        "code_lines": component.get("version", {}).get("cm_loc", [-1])[0],
        "average_cyclomatic_complexity":
            component.get("version", {}).get("cm_avg_cyclomatic_complexity", [-1])[0],
        "total_files": component.get("version", {}).get("cm_num_files", [-1])[0]
    }

    cves = []
    for cve in component.get("version", {}).get("cve_ids", []):
        component_cve = {
            'CVE': cve.split(':')[0],
            'CVSS': cve.split(':')[1]
        }
        cves.append(component_cve)

    licenses = component.get("version", {}).get("declared_licenses", [])
    name = component.get("version", {}).get("pname", [""])[0]
    version = component.get("version", {}).get("version", [""])[0]
    ecosystem = component.get("version", {}).get("pecosystem", [""])[0]
    latest_version = select_latest_version(
        version,
        component.get("package", {}).get("libio_latest_version", [""])[0],
        component.get("package", {}).get("latest_version", [""])[0],
        name
    )
    component_summary = {
        "ecosystem": ecosystem,
        "name": name,
        "version": version,
        "licenses": licenses,
        "security": cves,
        "osio_user_count": component.get("version", {}).get("osio_usage_count", 0),
        "latest_version": latest_version,
        "github": github_details,
        "code_metrics": code_metrics
    }

    return component_summary


def _extract_conflict_packages(license_service_output):
    """Extract conflict licenses.

    This helper function extracts conflict licenses from the given output
    of license analysis REST service.

    It returns a list of pairs of packages whose licenses are in conflict.
    Note that this information is only available when each component license
    was identified ( i.e. no unknown and no component level conflict ) and
    there was a stack level license conflict.

    :param license_service_output: output of license analysis REST service
    :return: list of pairs of packages whose licenses are in conflict
    """
    license_conflict_packages = []
    if not license_service_output:
        return license_conflict_packages

    conflict_packages = license_service_output.get('conflict_packages', [])
    for conflict_pair in conflict_packages:
        list_pkgs = list(conflict_pair.keys())
        assert len(list_pkgs) == 2
        d = {
            "package1": list_pkgs[0],
            "license1": conflict_pair[list_pkgs[0]],
            "package2": list_pkgs[1],
            "license2": conflict_pair[list_pkgs[1]]
        }
        license_conflict_packages.append(d)

    return license_conflict_packages


def _extract_unknown_licenses(license_service_output):
    """Extract unknown licenses.

    This helper function extracts unknown licenses information from the given
    output of license analysis REST service.

    At the moment, there are two types of unknowns:

    a. really unknown licenses: those licenses, which are not understood by our system.
    b. component level conflicting licenses: if a component has multiple licenses
        associated then license analysis service tries to identify a representative
        license for this component. If some licenses are in conflict, then its
        representative license cannot be identified and this is another type of
        'unknown' !

    This function returns both types of unknown licenses.

    :param license_service_output: output of license analysis REST service
    :return: list of packages with unknown licenses and/or conflicting licenses
    """
    # TODO: reduce cyclomatic complexity
    really_unknown_licenses = []
    lic_conflict_licenses = []
    if not license_service_output:
        return really_unknown_licenses

    # TODO: refactoring
    if license_service_output.get('status', '') == 'Unknown':
        list_components = license_service_output.get('packages', [])
        for comp in list_components:
            license_analysis = comp.get('license_analysis', {})
            if license_analysis.get('status', '') == 'Unknown':
                pkg = comp.get('package', 'Unknown')
                comp_unknown_licenses = license_analysis.get('unknown_licenses', [])
                for lic in comp_unknown_licenses:
                    really_unknown_licenses.append({
                        'package': pkg,
                        'license': lic
                    })

    # TODO: refactoring
    if license_service_output.get('status', '') == 'ComponentLicenseConflict':
        list_components = license_service_output.get('packages', [])
        for comp in list_components:
            license_analysis = comp.get('license_analysis', {})
            if license_analysis.get('status', '') == 'Conflict':
                pkg = comp.get('package', 'Unknown')
                d = {
                    "package": pkg
                }
                comp_conflict_licenses = license_analysis.get('conflict_licenses', [])
                list_conflicting_pairs = []
                for pair in comp_conflict_licenses:
                    assert (len(pair) == 2)
                    list_conflicting_pairs.append({
                        'license1': pair[0],
                        'license2': pair[1]
                    })
                d['conflict_licenses'] = list_conflicting_pairs
                lic_conflict_licenses.append(d)

    output = {
        'really_unknown': really_unknown_licenses,
        'component_conflict': lic_conflict_licenses
    }
    return output


def _extract_license_outliers(license_service_output):
    """Extract license outliers.

    This helper function extracts license outliers from the given output of
    license analysis REST service.

    :param license_service_output: output of license analysis REST service
    :return: list of license outlier packages
    """
    outliers = []
    if not license_service_output:
        return outliers

    outlier_packages = license_service_output.get('outlier_packages', {})
    for pkg in outlier_packages.keys():
        outliers.append({
            'package': pkg,
            'license': outlier_packages.get(pkg, 'Unknown')
        })

    return outliers


def perform_license_analysis(license_score_list, dependencies):
    """Pass given license_score_list to stack_license analysis and process response."""
    license_url = LICENSE_SCORING_URL_REST + "/api/v1/stack_license"

    payload = {
        "packages": license_score_list
    }
    resp = {}
    flag_stack_license_exception = False
    # TODO: refactoring
    try:
        lic_response = get_session_retry().post(license_url, data=json.dumps(payload))
        # lic_response.raise_for_status()  # raise exception for bad http-status codes
        resp = lic_response.json()
    except requests.exceptions.RequestException:
        current_app.logger.exception("Unexpected error happened while invoking license analysis!")
        flag_stack_license_exception = True

    stack_license = []
    stack_license_status = None
    unknown_licenses = []
    license_conflict_packages = []
    license_outliers = []
    if not flag_stack_license_exception:
        list_components = resp.get('packages', [])
        for comp in list_components:  # output from license analysis
            for dep in dependencies:  # the known dependencies
                if dep.get('name', '') == comp.get('package', '') and \
                                dep.get('version', '') == comp.get('version', ''):
                    dep['license_analysis'] = comp.get('license_analysis', {})

        _stack_license = resp.get('stack_license', None)
        if _stack_license is not None:
            stack_license = [_stack_license]
        stack_license_status = resp.get('status', None)
        unknown_licenses = _extract_unknown_licenses(resp)
        license_conflict_packages = _extract_conflict_packages(resp)
        license_outliers = _extract_license_outliers(resp)

    output = {
        "status": stack_license_status,
        "f8a_stack_licenses": stack_license,
        "unknown_licenses": unknown_licenses,
        "conflict_packages": license_conflict_packages,
        "outlier_packages": license_outliers
    }
    return output, dependencies


def extract_user_stack_package_licenses(resolved, ecosystem):
    """Extract user stack package licenses."""
    user_stack = get_dependency_data(resolved, ecosystem)
    list_package_licenses = []
    if user_stack is not None:
        for component in user_stack.get('result', []):
            data = component.get("data", None)
            if data:
                component_data = extract_component_details(data[0])
                license_scoring_input = {
                    'package': component_data['name'],
                    'version': component_data['version'],
                    'licenses': component_data['licenses']
                }
                list_package_licenses.append(license_scoring_input)

    return list_package_licenses


def aggregate_stack_data(stack, manifest_file, ecosystem, deps, manifest_file_path,
                         persist):
    """Aggregate stack data."""
    dependencies = []
    licenses = []
    license_score_list = []
    for component in stack.get('result', []):
        data = component.get("data", None)
        if data:
            component_data = extract_component_details(data[0])
            # create license dict for license scoring
            license_scoring_input = {
                'package': component_data['name'],
                'version': component_data['version'],
                'licenses': component_data['licenses']
            }
            dependencies.append(component_data)
            licenses.extend(component_data['licenses'])
            license_score_list.append(license_scoring_input)

    stack_distinct_licenses = set(licenses)

    # Call License Scoring to Get Stack License
    if persist:
        license_analysis, dependencies = perform_license_analysis(license_score_list, dependencies)
        stack_license_conflict = len(license_analysis.get('f8a_stack_licenses', [])) == 0
    else:
        license_analysis = dict()
        stack_license_conflict = None

    all_dependencies = {(dependency['package'], dependency['version']) for dependency in deps}
    analyzed_dependencies = {(dependency['name'], dependency['version'])
                             for dependency in dependencies}
    unknown_dependencies = list()
    for name, version in all_dependencies.difference(analyzed_dependencies):
        unknown_dependencies.append({'name': name, 'version': version})

    data = {
            "manifest_name": manifest_file,
            "manifest_file_path": manifest_file_path,
            "user_stack_info": {
                "ecosystem": ecosystem,
                "analyzed_dependencies_count": len(dependencies),
                "analyzed_dependencies": dependencies,
                "unknown_dependencies": unknown_dependencies,
                "unknown_dependencies_count": len(unknown_dependencies),
                "recommendation_ready": True,  # based on the percentage of dependencies analysed
                "total_licenses": len(stack_distinct_licenses),
                "distinct_licenses": list(stack_distinct_licenses),
                "stack_license_conflict": stack_license_conflict,
                "dependencies": deps,
                "license_analysis": license_analysis
            }
    }
    return data


def get_dependency_data(resolved, ecosystem):
    """Get dependency data from graph."""
    result = []
    for elem in resolved:
        if elem["package"] is None or elem["version"] is None:
            current_app.logger.warning("Either component name or component version is missing")
            continue

        qstring = \
            "g.V().has('pecosystem', '{}').has('pname', '{}').has('version', '{}')" \
            .format(ecosystem, elem["package"], elem["version"]) + \
            ".as('version').in('has_version').as('package')" + \
            ".select('version','package').by(valueMap());"
        payload = {'gremlin': qstring}

        try:
            graph_req = get_session_retry().post(GREMLIN_SERVER_URL_REST, data=json.dumps(payload))

            if graph_req.status_code == 200:
                graph_resp = graph_req.json()
                if 'result' not in graph_resp:
                    continue
                if len(graph_resp['result']['data']) == 0:
                    continue

                result.append(graph_resp["result"])
            else:
                current_app.logger.error("Failed retrieving dependency data.")
                continue
        except Exception:
            current_app.logger.exception("Error retrieving dependency data!")
            continue

    return {"result": result}


class StackAggregator:
    """Aggregate stack data from components."""

    @staticmethod
    def execute(aggregated=None, persist=True):
        """Task code."""
        started_at = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")
        finished = []
        stack_data = []
        external_request_id = aggregated.get('external_request_id')
        # TODO multiple license file support
        current_stack_license = aggregated.get('current_stack_license', {}).get('1', {})

        for result in aggregated['result']:
            resolved = result['details'][0]['_resolved']
            ecosystem = result['details'][0]['ecosystem']
            manifest = result['details'][0]['manifest_file']
            manifest_file_path = result['details'][0]['manifest_file_path']

            finished = get_dependency_data(resolved, ecosystem)
            if finished is not None:
                output = aggregate_stack_data(finished, manifest, ecosystem.lower(),
                                              resolved, manifest_file_path, persist)
                if output and output.get('user_stack_info'):
                    output['user_stack_info']['license_analysis'].update({
                        "current_stack_license": current_stack_license
                    })
                stack_data.append(output)

        ended_at = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")
        audit = {
            'started_at': started_at,
            'ended_at': ended_at,
            'version': 'v1'
        }
        stack_data = {
            'stack_data': stack_data,
            '_audit': audit,
            '_release': 'None:None:None'
        }
        if persist:
            # TODO: refactoring
            # Store the result in RDS
            try:
                insert_stmt = insert(WorkerResult).values(
                    worker='stack_aggregator_v2',
                    worker_id=None,
                    external_request_id=external_request_id,
                    analysis_id=None,
                    task_result=stack_data,
                    error=False
                )
                do_update_stmt = insert_stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_=dict(task_result=stack_data)
                )
                session.execute(do_update_stmt)
                session.commit()
                return {'stack_aggregator': 'success',
                        'external_request_id': external_request_id,
                        'result': stack_data}
            except SQLAlchemyError as e:
                session.rollback()
                return {
                    'stack_aggregator': 'database error',
                    'external_request_id': external_request_id,
                    'message': '%s' % e
                }
        else:
            return {'stack_aggregator': 'success',
                    'external_request_id': external_request_id,
                    'result': stack_data}
