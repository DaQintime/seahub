# Copyright (c) 2012-2016 Seafile Ltd.
import logging
import datetime

from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from seahub.api2.throttling import UserRateThrottle
from seahub.api2.authentication import TokenAuthentication
from seahub.api2.utils import api_error

from seahub.api2.endpoints.group_owned_libraries import get_group_id_by_repo_owner

from seahub.base.models import UserStarredFiles
from seahub.base.templatetags.seahub_tags import email2nickname, \
        email2contact_email
from seahub.signals import repo_deleted
from seahub.views import check_folder_permission, list_inner_pub_repos
from seahub.share.models import ExtraSharePermission
from seahub.group.utils import group_id_to_name
from seahub.utils import is_org_context, is_pro_version
from seahub.utils.timeutils import timestamp_to_isoformat_timestr
from seahub.utils.repo import get_repo_owner, is_repo_admin, \
        repo_has_been_shared_out, normalize_repo_status_code

from seahub.settings import ENABLE_STORAGE_CLASSES

from seaserv import seafile_api

logger = logging.getLogger(__name__)


class ReposView(APIView):

    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (UserRateThrottle,)

    def get(self, request):
        """ Return repos user can access.

        Permission checking:
        1. all authenticated user can perform this action.
        """

        filter_by = {
            'mine': False,
            'shared': False,
            'group': False,
            'public': False,
        }

        request_type_list = request.GET.getlist('type', "")
        if not request_type_list:
            # set all to True, no filter applied
            filter_by = filter_by.fromkeys(iter(filter_by.keys()), True)

        for request_type in request_type_list:
            request_type = request_type.strip()
            filter_by[request_type] = True

        email = request.user.username

        # Use dict to reduce memcache fetch cost in large for-loop.
        contact_email_dict = {}
        nickname_dict = {}

        org_id = None
        if is_org_context(request):
            org_id = request.user.org.org_id

        try:
            starred_repos = UserStarredFiles.objects.get_starred_repos_by_user(email)
            starred_repo_id_list = [item.repo_id for item in starred_repos]
        except Exception as e:
            logger.error(e)
            starred_repo_id_list = []

        repo_info_list = []
        if filter_by['mine']:

            if org_id:
                owned_repos = seafile_api.get_org_owned_repo_list(org_id,
                                                                  email,
                                                                  ret_corrupted=True)
            else:
                owned_repos = seafile_api.get_owned_repo_list(email,
                                                              ret_corrupted=True)

            # Reduce memcache fetch ops.
            modifiers_set = {x.last_modifier for x in owned_repos}
            for e in modifiers_set:
                if e not in contact_email_dict:
                    contact_email_dict[e] = email2contact_email(e)
                if e not in nickname_dict:
                    nickname_dict[e] = email2nickname(e)

            owned_repos.sort(key=lambda x: x.last_modify, reverse=True)
            for r in owned_repos:

                # do not return virtual repos
                if r.is_virtual:
                    continue

                repo_info = {
                    "type": "mine",
                    "repo_id": r.id,
                    "repo_name": r.name,
                    "owner_email": email,
                    "owner_name": email2nickname(email),
                    "owner_contact_email": email2contact_email(email),
                    "last_modified": timestamp_to_isoformat_timestr(r.last_modify),
                    "modifier_email": r.last_modifier,
                    "modifier_name": nickname_dict.get(r.last_modifier, ''),
                    "modifier_contact_email": contact_email_dict.get(r.last_modifier, ''),
                    "size": r.size,
                    "encrypted": r.encrypted,
                    "permission": 'rw',  # Always have read-write permission to owned repo
                    "starred": r.repo_id in starred_repo_id_list,
                    "status": normalize_repo_status_code(r.status),
                    "salt": r.salt if r.enc_version >= 3 else '',
                }

                if is_pro_version() and ENABLE_STORAGE_CLASSES:
                    repo_info['storage_name'] = r.storage_name
                    repo_info['storage_id'] = r.storage_id

                repo_info_list.append(repo_info)

        if filter_by['shared']:

            if org_id:
                shared_repos = seafile_api.get_org_share_in_repo_list(org_id,
                                                                      email, -1, -1)
            else:
                shared_repos = seafile_api.get_share_in_repo_list(email, -1, -1)

            repos_with_admin_share_to = ExtraSharePermission.objects.get_repos_with_admin_permission(email)

            # Reduce memcache fetch ops.
            owners_set = {x.user for x in shared_repos}
            modifiers_set = {x.last_modifier for x in shared_repos}
            for e in owners_set | modifiers_set:
                if e not in contact_email_dict:
                    contact_email_dict[e] = email2contact_email(e)
                if e not in nickname_dict:
                    nickname_dict[e] = email2nickname(e)

            shared_repos.sort(key=lambda x: x.last_modify, reverse=True)
            for r in shared_repos:

                owner_email = r.user

                group_name = ''
                is_group_owned_repo = False
                if '@seafile_group' in owner_email:
                    is_group_owned_repo = True
                    group_id = get_group_id_by_repo_owner(owner_email)
                    group_name = group_id_to_name(group_id)

                owner_name = group_name if is_group_owned_repo else nickname_dict.get(owner_email, '')
                owner_contact_email = '' if is_group_owned_repo else contact_email_dict.get(owner_email, '')

                repo_info = {
                    "type": "shared",
                    "repo_id": r.repo_id,
                    "repo_name": r.repo_name,
                    "last_modified": timestamp_to_isoformat_timestr(r.last_modify),
                    "modifier_email": r.last_modifier,
                    "modifier_name": nickname_dict.get(r.last_modifier, ''),
                    "modifier_contact_email": contact_email_dict.get(r.last_modifier, ''),
                    "owner_email": owner_email,
                    "owner_name": owner_name,
                    "owner_contact_email": owner_contact_email,
                    "size": r.size,
                    "encrypted": r.encrypted,
                    "permission": r.permission,
                    "starred": r.repo_id in starred_repo_id_list,
                    "status": normalize_repo_status_code(r.status),
                    "salt": r.salt if r.enc_version >= 3 else '',
                }

                if r.repo_id in repos_with_admin_share_to:
                    repo_info['is_admin'] = True
                else:
                    repo_info['is_admin'] = False

                repo_info_list.append(repo_info)

        if filter_by['group']:

            if org_id:
                group_repos = seafile_api.get_org_group_repos_by_user(email, org_id)
            else:
                group_repos = seafile_api.get_group_repos_by_user(email)

            group_repos.sort(key=lambda x: x.last_modify, reverse=True)

            # Reduce memcache fetch ops.
            share_from_set = {x.user for x in group_repos}
            modifiers_set = {x.last_modifier for x in group_repos}
            for e in modifiers_set | share_from_set:
                if e not in contact_email_dict:
                    contact_email_dict[e] = email2contact_email(e)
                if e not in nickname_dict:
                    nickname_dict[e] = email2nickname(e)

            for r in group_repos:
                repo_info = {
                    "type": "group",
                    "group_id": r.group_id,
                    "group_name": r.group_name,
                    "repo_id": r.repo_id,
                    "repo_name": r.repo_name,
                    "last_modified": timestamp_to_isoformat_timestr(r.last_modify),
                    "modifier_email": r.last_modifier,
                    "modifier_name": nickname_dict.get(r.last_modifier, ''),
                    "modifier_contact_email": contact_email_dict.get(r.last_modifier, ''),
                    "size": r.size,
                    "encrypted": r.encrypted,
                    "permission": r.permission,
                    "starred": r.repo_id in starred_repo_id_list,
                    "status": normalize_repo_status_code(r.status),
                    "salt": r.salt if r.enc_version >= 3 else '',
                }
                repo_info_list.append(repo_info)

        if filter_by['public'] and request.user.permissions.can_view_org():
            public_repos = list_inner_pub_repos(request)

            # get repo id owner dict
            all_repo_owner = []
            repo_id_owner_dict = {}
            for repo in public_repos:
                repo_id = repo.repo_id
                if repo_id not in repo_id_owner_dict:
                    repo_owner = get_repo_owner(request, repo_id)
                    all_repo_owner.append(repo_owner)
                    repo_id_owner_dict[repo_id] = repo_owner

            # Reduce memcache fetch ops.
            owner_set = set(all_repo_owner)
            share_from_set = {x.user for x in public_repos}
            modifiers_set = {x.last_modifier for x in public_repos}
            for e in modifiers_set | share_from_set | owner_set:
                if e not in contact_email_dict:
                    contact_email_dict[e] = email2contact_email(e)
                if e not in nickname_dict:
                    nickname_dict[e] = email2nickname(e)

            for r in public_repos:
                repo_owner = repo_id_owner_dict[r.repo_id]
                repo_info = {
                    "type": "public",
                    "repo_id": r.repo_id,
                    "repo_name": r.repo_name,
                    "last_modified": timestamp_to_isoformat_timestr(r.last_modify),
                    "modifier_email": r.last_modifier,
                    "modifier_name": nickname_dict.get(r.last_modifier, ''),
                    "modifier_contact_email": contact_email_dict.get(r.last_modifier, ''),
                    "owner_email": repo_owner,
                    "owner_name": nickname_dict.get(repo_owner, ''),
                    "owner_contact_email": contact_email_dict.get(repo_owner, ''),
                    "size": r.size,
                    "encrypted": r.encrypted,
                    "permission": r.permission,
                    "starred": r.repo_id in starred_repo_id_list,
                    "status": normalize_repo_status_code(r.status),
                    "salt": r.salt if r.enc_version >= 3 else '',
                }
                repo_info_list.append(repo_info)

        utc_dt = datetime.datetime.utcnow()
        timestamp = utc_dt.strftime('%Y-%m-%d %H:%M:%S')
        org_id = request.user.org.org_id if is_org_context(request) else -1
        try:
            seafile_api.publish_event('seahub.stats', 'user-login\t%s\t%s\t%s' % (email, timestamp, org_id))
        except Exception as e:
            logger.error('Error when sending user-login message: %s' % str(e))

        return Response({'repos': repo_info_list})


class RepoView(APIView):

    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (UserRateThrottle,)

    def get(self, request, repo_id):
        """ Return repo info

        Permission checking:
        1. all authenticated user can perform this action.
        """

        # resource check
        repo = seafile_api.get_repo(repo_id)
        if not repo:
            error_msg = 'Library %s not found.' % repo_id
            return api_error(status.HTTP_404_NOT_FOUND, error_msg)

        # permission check
        permission = check_folder_permission(request, repo_id, '/')
        if permission is None:
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        username = request.user.username

        lib_need_decrypt = False
        if repo.encrypted \
                and not seafile_api.is_password_set(repo.id, username):
            lib_need_decrypt = True

        repo_owner = get_repo_owner(request, repo_id)

        try:
            has_been_shared_out = repo_has_been_shared_out(request, repo_id)
        except Exception as e:
            has_been_shared_out = False
            logger.error(e)

        result = {
            "repo_id": repo.id,
            "repo_name": repo.name,

            "owner_email": repo_owner,
            "owner_name": email2nickname(repo_owner),
            "owner_contact_email": email2contact_email(repo_owner),

            "size": repo.size,
            "encrypted": repo.encrypted,
            "file_count": repo.file_count,
            "permission": permission,
            "no_quota": True if seafile_api.check_quota(repo_id) < 0 else False,
            "is_admin": is_repo_admin(username, repo_id),
            "is_virtual": repo.is_virtual,
            "has_been_shared_out": has_been_shared_out,

            "lib_need_decrypt": lib_need_decrypt,
            "last_modified": timestamp_to_isoformat_timestr(repo.last_modify),
            "status": normalize_repo_status_code(repo.status),
        }

        return Response(result)

    def delete(self, request, repo_id):

        repo = seafile_api.get_repo(repo_id)
        if not repo:
            # for case of `seafile-data` has been damaged
            # no `repo object` will be returned from seafile api
            # delete the database record anyway
            try:
                seafile_api.remove_repo(repo_id)
            except Exception as e:
                logger.error(e)
                error_msg = 'Internal Server Error'
                return api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, error_msg)

            return Response({'success': True})

        # check permission
        username = request.user.username
        repo_owner = get_repo_owner(request, repo_id)
        if username != repo_owner:
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        # check repo status
        repo_status = repo.status
        if repo_status != 0:
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        org_id = None
        if is_org_context(request):
            org_id = request.user.org.org_id

        # remove repo
        seafile_api.remove_repo(repo_id)

        repo_deleted.send(sender=None,
                          org_id=org_id,
                          operator=username,
                          repo_owner=repo_owner,
                          repo_id=repo_id,
                          repo_name=repo.name)

        return Response('success', status=status.HTTP_200_OK)


class RepoShareInfoView(APIView):

    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (UserRateThrottle,)

    def get(self, request, repo_id):
        """ Return repo share info

        Permission checking:
        1. all authenticated user can perform this action.
        """

        # resource check
        repo = seafile_api.get_repo(repo_id)
        if not repo:
            error_msg = 'Library %s not found.' % repo_id
            return api_error(status.HTTP_404_NOT_FOUND, error_msg)

        # permission check
        permission = check_folder_permission(request, repo_id, '/')
        if not permission:
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        folder_path = request.GET.get('path', '/')
        if folder_path != '/':
            dir_id = seafile_api.get_dir_id_by_path(repo_id, folder_path)
            if not dir_id:
                error_msg = 'Folder %s not found.' % folder_path
                return api_error(status.HTTP_404_NOT_FOUND, error_msg)

        if is_org_context(request):
            org_id = request.user.org.org_id
            repo_owner = seafile_api.get_org_repo_owner(repo_id)
            if folder_path == '/':
                shared_users = seafile_api.list_org_repo_shared_to(repo_owner, repo_id)
                shared_groups = seafile_api.list_org_repo_shared_group(org_id, repo_owner, repo_id)
            else:
                shared_users = seafile_api.get_org_shared_users_for_subdir(org_id,
                                                                           repo_id,
                                                                           folder_path,
                                                                           repo_owner)
                shared_groups = seafile_api.get_org_shared_groups_for_subdir(org_id,
                                                                             repo_id,
                                                                             folder_path,
                                                                             repo_owner)
        else:
            repo_owner = seafile_api.get_repo_owner(repo_id)
            if folder_path == '/':
                shared_users = seafile_api.list_repo_shared_to(repo_owner, repo_id)
                shared_groups = seafile_api.list_repo_shared_group_by_user(repo_owner, repo_id)
            else:
                shared_users = seafile_api.get_shared_users_for_subdir(repo_id,
                                                                       folder_path,
                                                                       repo_owner)
                shared_groups = seafile_api.get_shared_groups_for_subdir(repo_id,
                                                                         folder_path,
                                                                         repo_owner)

        group_id = ''
        if '@seafile_group' in repo_owner:
            group_id = get_group_id_by_repo_owner(repo_owner)

        shared_group_ids = [item.group_id for item in shared_groups]
        if group_id and group_id in shared_group_ids:
            shared_group_ids.remove(group_id)

        result = {
            "shared_user_emails": [item.user for item in shared_users],
            "shared_group_ids": shared_group_ids,
        }

        return Response(result)
