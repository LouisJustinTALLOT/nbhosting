from django.shortcuts import render
# from django.http import HttpResponse, HttpResponseNotFound, HttpResponseRedirect

from django.views.decorators.csrf import csrf_protect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout as django_logout

from docker.errors import DockerException


from nbhosting.version import __version__ as nbh_version_global

from nbhosting.students.models import Student

@csrf_protect
def welcome(request):
#    that welcome page is for devel purposes only
    return render(request, 'welcome.html',
                  dict(nbh_version=nbh_version_global))


@csrf_protect
def logout(request):
    """
    in addition to the regular django log out process, this 
    hooks allows to kill all containers currently running 
    as the current user's behalf
    """

    student = Student(request.user.username)
    nbh_version = nbh_version_global
    
    try:
        containers_before = student.spot_running_containers()
        nb_containers_before = len(containers_before)
        student.kill_running_containers(containers=containers_before)
        containers_after = student.spot_running_containers()
        nb_containers_after = len(containers_after)
        docker_ok = True
    except DockerException as exc:
        docker_ok = False
        docker_exception = exc

    django_logout(request)
    return render(request, "registration/logged_out.html",
                  locals())