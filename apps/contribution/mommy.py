import json
import logging

import requests
from django.conf import settings
from django.utils import timezone
from pytz import timezone as tz

from apps.contribution.models import (ActivityEntry, Contribution, ExternalContributor, Repository,
                                      RepositoryLanguage)
from apps.mommy import schedule
from apps.mommy.registry import Task

logger = logging.getLogger()


class UpdateRepositories(Task):

    @staticmethod
    def run():
        """
        if settings.GITHUB_GRAPHQL_TOKEN == "no_token":
            logger.error("GraphQL token did not exist. Contribution.mommy was unable to execute.")
            return
        """

        # Load new data
        fresh = UpdateRepositories.git_repositories()
        if fresh is None:
            return

        dotkom_members = UpdateRepositories.dotkom_members()

        localtz = tz('Europe/Oslo')
        for repo in fresh:
            fresh_repo = Repository(
                id=repo['id'],
                name=repo['name'],
                description=repo['description'],
                updated_at=localtz.localize(timezone.datetime.strptime(repo['updated_at'], "%Y-%m-%dT%H:%M:%SZ")),
                public_url=repo['public_url'],
                issues=repo['issues']
            )

            repo_languages = repo['languages']
            activity_data = repo['activity']

            # If repository exists, only update data
            if Repository.objects.filter(id=fresh_repo.id).exists():
                stored_repo = Repository.objects.get(id=fresh_repo.id)
                UpdateRepositories.update_repository(stored_repo, fresh_repo, repo_languages, activity_data,
                                                     dotkom_members)
            # else: repository does not exist
            else:
                UpdateRepositories.new_repository(fresh_repo, repo_languages, activity_data, dotkom_members)

        # Delete repositories that does not satisfy the updated_at limit
        old_repositories = Repository.objects.all()
        for repo in old_repositories:
            if repo.updated_at < timezone.now() - timezone.timedelta(days=730):
                repo.delete()

    @staticmethod
    def update_repository(stored_repo, fresh_repo, repo_languages, activity_data, dotkom_members):
        stored_repo.name = fresh_repo.name
        stored_repo.description = fresh_repo.description
        stored_repo.updated_at = fresh_repo.updated_at
        stored_repo.public_url = fresh_repo.public_url
        stored_repo.issues = fresh_repo.issues
        stored_repo.save()

        # Update languages if they exist, and add if not
        for language in repo_languages:
            if RepositoryLanguage.objects.filter(type=language['name'], repository=stored_repo).exists():
                stored_language = RepositoryLanguage.objects.get(type=language['name'], repository=stored_repo)
                stored_language.size = language['size']
                stored_language.color = language['color']
                stored_language.save()
            else:
                new_language = RepositoryLanguage(
                    type=language['name'],
                    size=language['size'],
                    color=language['color'],
                    repository=stored_repo
                )
                new_language.save()

        # Add activity data
        UpdateRepositories.update_activity(stored_repo, activity_data)

        # update repository contributors
        contributors = UpdateRepositories.get_contributors(stored_repo.name)
        for username in contributors:
            if username not in dotkom_members:
                if ExternalContributor.objects.filter(username=username).exists():
                    contributor = ExternalContributor.objects.get(username=username)

                    if Contribution.objects.filter(contributor=contributor, repository=stored_repo).exists():
                        contribution = Contribution.objects.get(contributor=contributor, repository=stored_repo)
                        contribution.commits = contributors[username]
                        contribution.save()
                    else:
                        new_contribution = Contribution(
                            contributor=contributor,
                            repository=stored_repo,
                            commits=contributors[username]
                        )
                        new_contribution.save()
                else:
                    new_contributor = ExternalContributor(username=username)
                    new_contributor.save()

                    new_contribution = Contribution(
                        contributor=new_contributor,
                        repository=stored_repo,
                        commits=contributors[username]
                    )
                    new_contribution.save()

    @staticmethod
    def new_repository(new_repo, new_languages, activity_data, dotkom_members):
        # Filter out repositories with inactivity past 2 years (365 days * 2)
        if new_repo.updated_at > timezone.now() - timezone.timedelta(days=730):
            new_repo = Repository(
                id=new_repo.id,
                name=new_repo.name,
                description=new_repo.description,
                updated_at=new_repo.updated_at,
                public_url=new_repo.public_url,
                issues=new_repo.issues
            )
            new_repo.save()

            # Add repository languages
            for language in new_languages:
                new_language = RepositoryLanguage(
                    type=language['name'],
                    size=language['size'],
                    color=language['color'],
                    repository=new_repo
                )
                new_language.save()

            # Add activity data
            UpdateRepositories.update_activity(new_repo, activity_data)

            # new repository contributors
            contributors = UpdateRepositories.get_contributors(new_repo.name)
            for username in contributors:
                if username not in dotkom_members:
                    if ExternalContributor.objects.filter(username=username).exists():
                        contributor = ExternalContributor.objects.get(username=username)
                    else:
                        contributor = ExternalContributor(username=username)
                        contributor.save()

                    contribution = Contribution(
                        contributor=contributor,
                        repository=new_repo,
                        commits=contributors[username]
                    )
                    contribution.save()

    @staticmethod
    def update_activity(repository, activity_data):
        # Calculate activity scores on dates
        parsed_entries = {}
        for entry in activity_data:
            date = timezone.datetime.strptime(entry['datetime'], "%Y-%m-%dT%H:%M:%SZ").date()
            activity_score = int(entry['additions']) + int(entry['deletions'])

            if date not in parsed_entries:
                parsed_entries[date] = 0
            parsed_entries[date] += activity_score

        # Only add data if it doesn't already exist
        for date in parsed_entries:
            if not ActivityEntry.objects.filter(date=date).exists():
                activity_entry = ActivityEntry(
                    repository=repository,
                    date=date,
                    score=parsed_entries[date]
                )
                activity_entry.save()

    @staticmethod
    def git_repositories():
        query = """
        query($since: GitTimestamp) {
          organization(login: "dotkom") {
            repositories (first: 100) {
              nodes {
                id
                name
                description
                updatedAt
                url
                issues (states: [OPEN]){
                  totalCount
                }
                defaultBranchRef{
                  target{
                    ... on Commit{
                      history(first: 500, since: $since){
                        nodes {
                          committedDate
                          additions
                          deletions
                        }
                      }
                    }
                  }
                }
                isPrivate
                languages (first:10) {
                  totalSize
                  edges {
                    size
                    node {
                      name
                      color
                    }
                  }
                }
              }
            }
          }
        }
        """
        time = timezone.datetime.strftime(timezone.now() - timezone.timedelta(days=7), "%Y-%m-%dT%H:%M:%SZ")
        variables = {
            'since': str(time)
        }

        response = UpdateRepositories.post_graphql(query, variables)
        if response.get('data') is None:
            logger.error("Something went wrong: {}".format(response.get('errors')))
            return None

        dict_repos = response.get('data', {}).get('organization', {}).get('repositories', {}).get('nodes')

        repository_list = []
        for r in dict_repos:

            # If repository is private, skip
            if r.get('isPrivate'):
                continue

            # Parse language data
            total_size = r.get('languages', {}).get('totalSize')
            languages = []
            for l in r.get('languages', {}).get('edges'):
                language = {
                    'name': l.get('node', {}).get('name'),
                    'color': l.get('node', {}).get('color'),
                    'size': l.get('size')
                }
                languages.append(language)

            # Parse activity data
            activities = []
            for entry in r.get('defaultBranchRef', {}).get('target', {}).get('history', {}).get('nodes'):
                activity = {
                    'datetime': entry.get('committedDate'),
                    'additions': entry.get('additions'),
                    'deletions': entry.get('deletions')
                }
                activities.append(activity)

            # Parse repository data
            repo = {
                'id': r.get('id'),
                'name': r.get('name'),
                'description': r.get('description'),
                'updated_at': r.get('updatedAt'),
                'public_url': r.get('url'),
                'issues': r.get('issues', {}).get('totalCount'),
                'total_size': total_size,
                'languages': languages,
                'activity': activities
            }
            repository_list.append(repo)

        return repository_list

    @staticmethod
    def dotkom_members():
        query = """
                {
                  organization(login: "dotkom") {
                    members (first: 100) {
                      nodes {
                        login
                      }
                    }
                  }
                }
                """
        response = UpdateRepositories.post_graphql(query)
        members = response.get('data', {}).get('organization', {}).get('members', {}).get('nodes')

        member_list = []
        for member in members:
            member_list.append(member.get('login'))
        return member_list

    # RestV3 for contributors, as GraphQL does not support this yet
    # Returns dictionary { user: contributions }
    @staticmethod
    def get_contributors(repo_name):
        url = "https://api.github.com/repos/dotkom/{0}/contributors?per_page=100".format(repo_name)
        r = requests.get(url)
        data = json.loads(r.text)

        contributors = {}
        for user in data:
            username = str(user['login'])
            commits = int(user['contributions'])
            contributors[username] = commits
        return contributors

    # GraphQL post method
    @staticmethod
    def post_graphql(query, variables=None):
        token = settings.GITHUB_GRAPHQL_TOKEN
        url = "https://api.github.com/graphql"

        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'bearer ' + str(token)
        }

        r = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
        return json.loads(r.text)

schedule.register(UpdateRepositories, day_of_week="mon-sun", hour=13, minute=52)