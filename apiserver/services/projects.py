from datetime import datetime

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apierrors.errors.bad_request import InvalidProjectId
from apiserver.apimodels.base import UpdateResponse, MakePublicRequest, IdResponse
from apiserver.apimodels.projects import (
    GetHyperParamReq,
    ProjectReq,
    ProjectTagsRequest,
    ProjectTaskParentsRequest,
    ProjectHyperparamValuesRequest,
    ProjectsGetRequest,
)
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project import ProjectBLL
from apiserver.bll.task import TaskBLL
from apiserver.database.errors import translate_errors_context
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task
from apiserver.database.utils import (
    parse_from_call,
    get_company_or_none_constraint,
)
from apiserver.service_repo import APICall, endpoint
from apiserver.services.utils import (
    conform_tag_fields,
    conform_output_tags,
    get_tags_filter_dictionary,
    get_tags_response,
)
from apiserver.timing_context import TimingContext

org_bll = OrgBLL()
task_bll = TaskBLL()
project_bll = ProjectBLL()

create_fields = {
    "name": None,
    "description": None,
    "tags": list,
    "system_tags": list,
    "default_output_destination": None,
}

get_all_query_options = Project.QueryParameterOptions(
    pattern_fields=("name", "description"), list_fields=("tags", "system_tags", "id"),
)


@endpoint("projects.get_by_id", required_fields=["project"])
def get_by_id(call):
    assert isinstance(call, APICall)
    project_id = call.data["project"]

    with translate_errors_context():
        with TimingContext("mongo", "projects_by_id"):
            query = Q(id=project_id) & get_company_or_none_constraint(
                call.identity.company
            )
            project = Project.objects(query).first()
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        project_dict = project.to_proper_dict()
        conform_output_tags(call, project_dict)

        call.result.data = {"project": project_dict}


@endpoint("projects.get_all_ex", request_data_model=ProjectsGetRequest)
def get_all_ex(call: APICall, company_id: str, request: ProjectsGetRequest):
    conform_tag_fields(call, call.data)
    allow_public = not request.non_public
    with TimingContext("mongo", "projects_get_all"):
        if request.active_users:
            ids = project_bll.get_projects_with_active_user(
                company=company_id,
                users=request.active_users,
                project_ids=call.data.get("id"),
                allow_public=allow_public,
            )
            if not ids:
                call.result.data = {"projects": []}
                return
            call.data["id"] = ids

        projects = Project.get_many_with_join(
            company=company_id,
            query_dict=call.data,
            query_options=get_all_query_options,
            allow_public=allow_public,
        )

        conform_output_tags(call, projects)
        if not request.include_stats:
            call.result.data = {"projects": projects}
            return

        project_ids = {project["id"] for project in projects}
        stats = project_bll.get_project_stats(
            company=company_id,
            project_ids=list(project_ids),
            specific_state=request.stats_for_state,
        )

        for project in projects:
            project["stats"] = stats[project["id"]]

        call.result.data = {"projects": projects}


@endpoint("projects.get_all")
def get_all(call: APICall):
    conform_tag_fields(call, call.data)
    with translate_errors_context(), TimingContext("mongo", "projects_get_all"):
        projects = Project.get_many(
            company=call.identity.company,
            query_dict=call.data,
            query_options=get_all_query_options,
            parameters=call.data,
            allow_public=True,
        )
        conform_output_tags(call, projects)

        call.result.data = {"projects": projects}


@endpoint(
    "projects.create",
    required_fields=["name", "description"],
    response_data_model=IdResponse,
)
def create(call: APICall):
    identity = call.identity

    with translate_errors_context():
        fields = parse_from_call(call.data, create_fields, Project.get_fields())
        conform_tag_fields(call, fields, validate=True)

        return IdResponse(
            id=ProjectBLL.create(
                user=identity.user, company=identity.company, **fields,
            )
        )


@endpoint(
    "projects.update", required_fields=["project"], response_data_model=UpdateResponse
)
def update(call: APICall):
    """
    update

    :summary: Update project information.
              See `project.create` for parameters.
    :return: updated - `int` - number of projects updated
             fields - `[string]` - updated fields
    """
    project_id = call.data["project"]

    with translate_errors_context():
        project = Project.get_for_writing(company=call.identity.company, id=project_id)
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        fields = parse_from_call(
            call.data, create_fields, Project.get_fields(), discard_none_values=False
        )
        conform_tag_fields(call, fields, validate=True)
        fields["last_update"] = datetime.utcnow()
        with TimingContext("mongo", "projects_update"):
            updated = project.update(upsert=False, **fields)
        conform_output_tags(call, fields)
        call.result.data_model = UpdateResponse(updated=updated, fields=fields)


@endpoint("projects.delete", required_fields=["project"])
def delete(call):
    assert isinstance(call, APICall)
    project_id = call.data["project"]
    force = call.data.get("force", False)

    with translate_errors_context():
        project = Project.get_for_writing(company=call.identity.company, id=project_id)
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        # NOTE: from this point on we'll use the project ID and won't check for company, since we assume we already
        # have the correct project ID.

        # Find the tasks which belong to the project
        for cls, error in (
            (Task, errors.bad_request.ProjectHasTasks),
            (Model, errors.bad_request.ProjectHasModels),
        ):
            res = cls.objects(
                project=project_id, system_tags__nin=[EntityVisibility.archived.value]
            ).only("id")
            if res and not force:
                raise error("use force=true to delete", id=project_id)

        updated_count = res.update(project=None)

        project.delete()

        call.result.data = {"deleted": 1, "disassociated_tasks": updated_count}


@endpoint("projects.get_unique_metric_variants", request_data_model=ProjectReq)
def get_unique_metric_variants(call: APICall, company_id: str, request: ProjectReq):

    metrics = task_bll.get_unique_metric_variants(
        company_id, [request.project] if request.project else None
    )

    call.result.data = {"metrics": metrics}


@endpoint(
    "projects.get_hyper_parameters",
    min_version="2.9",
    request_data_model=GetHyperParamReq,
)
def get_hyper_parameters(call: APICall, company_id: str, request: GetHyperParamReq):

    total, remaining, parameters = TaskBLL.get_aggregated_project_parameters(
        company_id,
        project_ids=[request.project] if request.project else None,
        page=request.page,
        page_size=request.page_size,
    )

    call.result.data = {
        "total": total,
        "remaining": remaining,
        "parameters": parameters,
    }


@endpoint(
    "projects.get_hyperparam_values",
    min_version="2.13",
    request_data_model=ProjectHyperparamValuesRequest,
)
def get_hyperparam_values(
    call: APICall, company_id: str, request: ProjectHyperparamValuesRequest
):
    total, values = task_bll.get_hyperparam_distinct_values(
        company_id,
        project_ids=request.projects,
        section=request.section,
        name=request.name,
        allow_public=request.allow_public,
    )
    call.result.data = {
        "total": total,
        "values": values,
    }


@endpoint(
    "projects.get_task_tags", min_version="2.8", request_data_model=ProjectTagsRequest
)
def get_tags(call: APICall, company, request: ProjectTagsRequest):
    ret = org_bll.get_tags(
        company,
        Tags.Task,
        include_system=request.include_system,
        filter_=get_tags_filter_dictionary(request.filter),
        projects=request.projects,
    )
    call.result.data = get_tags_response(ret)


@endpoint(
    "projects.get_model_tags", min_version="2.8", request_data_model=ProjectTagsRequest
)
def get_tags(call: APICall, company, request: ProjectTagsRequest):
    ret = org_bll.get_tags(
        company,
        Tags.Model,
        include_system=request.include_system,
        filter_=get_tags_filter_dictionary(request.filter),
        projects=request.projects,
    )
    call.result.data = get_tags_response(ret)


@endpoint(
    "projects.make_public", min_version="2.9", request_data_model=MakePublicRequest
)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Project.set_public(
        company_id, ids=request.ids, invalid_cls=InvalidProjectId, enabled=True
    )


@endpoint(
    "projects.make_private", min_version="2.9", request_data_model=MakePublicRequest
)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Project.set_public(
        company_id, ids=request.ids, invalid_cls=InvalidProjectId, enabled=False
    )


@endpoint(
    "projects.get_task_parents",
    min_version="2.12",
    request_data_model=ProjectTaskParentsRequest,
)
def get_task_parents(
    call: APICall, company_id: str, request: ProjectTaskParentsRequest
):
    call.result.data = {
        "parents": org_bll.get_parent_tasks(
            company_id, projects=request.projects, state=request.tasks_state
        )
    }
