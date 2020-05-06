"""
The ``mlflow.projects`` module provides an API for running MLflow projects locally or remotely.
"""
import hashlib
import json
import yaml
import os
import sys
import shutil
from six.moves import urllib
import subprocess
import tempfile
import logging
import posixpath
import docker
import platform

import mlflow.projects.databricks
import mlflow.tracking as tracking
from mlflow.entities import RunStatus
from mlflow.exceptions import ExecutionException, MlflowException
from mlflow.projects.submitted_run import LocalSubmittedRun, SubmittedRun
from mlflow.projects.utils import (
    _get_storage_dir, fetch_and_validate_project, get_or_create_run, load_project,
    _MLFLOW_LOCAL_BACKEND_RUN_ID_CONFIG
)
from mlflow.projects.backend import loader
from mlflow.tracking.context.git_context import _get_git_commit
from mlflow.tracking.fluent import _get_experiment_id
from mlflow.store.artifact.artifact_repository_registry import get_artifact_repository
from mlflow.store.artifact.azure_blob_artifact_repo import AzureBlobArtifactRepository
from mlflow.store.artifact.gcs_artifact_repo import GCSArtifactRepository
from mlflow.store.artifact.hdfs_artifact_repo import HdfsArtifactRepository
from mlflow.store.artifact.local_artifact_repo import LocalArtifactRepository
from mlflow.store.artifact.s3_artifact_repo import S3ArtifactRepository
from mlflow.utils import databricks_utils, file_utils, process
from mlflow.utils.file_utils import path_to_local_sqlite_uri, path_to_local_file_uri
from mlflow.utils.mlflow_tags import (
    MLFLOW_PROJECT_ENV, MLFLOW_DOCKER_IMAGE_URI, MLFLOW_DOCKER_IMAGE_ID, MLFLOW_PROJECT_BACKEND,
)
from mlflow.utils.uri import get_db_profile_from_uri, is_databricks_uri

# Environment variable indicating a path to a conda installation. MLflow will default to running
# "conda" if unset
MLFLOW_CONDA_HOME = "MLFLOW_CONDA_HOME"
_GENERATED_DOCKERFILE_NAME = "Dockerfile.mlflow-autogenerated"
_PROJECT_TAR_ARCHIVE_NAME = "mlflow-project-docker-build-context"
_MLFLOW_DOCKER_TRACKING_DIR_PATH = "/mlflow/tmp/mlruns"
_MLFLOW_DOCKER_WORKDIR_PATH = "/mlflow/projects/code/"

_logger = logging.getLogger(__name__)


def _resolve_experiment_id(experiment_name=None, experiment_id=None):
    """
    Resolve experiment.

    Verifies either one or other is specified - cannot be both selected.

    If ``experiment_name`` is provided and does not exist, an experiment
    of that name is created and its id is returned.

    :param experiment_name: Name of experiment under which to launch the run.
    :param experiment_id: ID of experiment under which to launch the run.
    :return: str
    """

    if experiment_name and experiment_id:
        raise MlflowException("Specify only one of 'experiment_name' or 'experiment_id'.")

    if experiment_id:
        return str(experiment_id)

    if experiment_name:
        client = tracking.MlflowClient()
        exp = client.get_experiment_by_name(experiment_name)
        if exp:
            return exp.experiment_id
        else:
            print("INFO: '{}' does not exist. Creating a new experiment".format(experiment_name))
            return client.create_experiment(experiment_name)

    return _get_experiment_id()


def _run(uri, experiment_id, entry_point="main", version=None, parameters=None,
         docker_args=None, backend_name=None, backend_config=None, use_conda=True,
         storage_dir=None, synchronous=True):
    """
    Helper that delegates to the project-running method corresponding to the passed-in backend.
    Returns a ``SubmittedRun`` corresponding to the project run.
    """
    tracking_store_uri = tracking.get_tracking_uri()
    if backend_name:
        backend = loader.load_backend(backend_name)
        if backend:
            submitted_run = backend.run(uri, entry_point, parameters,
                                        version, backend_config, experiment_id, tracking_store_uri)
            tracking.MlflowClient().set_tag(submitted_run.run_id, MLFLOW_PROJECT_BACKEND,
                                            backend_name)
            return submitted_run

    work_dir = fetch_and_validate_project(uri, version, entry_point, parameters)
    project = load_project(work_dir)
    _validate_execution_environment(project, backend_name)

    existing_run_id = None
    if backend_name == "local" and _MLFLOW_LOCAL_BACKEND_RUN_ID_CONFIG in backend_config:
        existing_run_id = backend_config[_MLFLOW_LOCAL_BACKEND_RUN_ID_CONFIG]
    active_run = get_or_create_run(existing_run_id, uri, experiment_id, work_dir, version,
                                   entry_point, parameters)

    if backend_name == "databricks":
        tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_BACKEND,
                                        "databricks")
        from mlflow.projects.databricks import run_databricks
        return run_databricks(
            remote_run=active_run,
            uri=uri, entry_point=entry_point, work_dir=work_dir, parameters=parameters,
            experiment_id=experiment_id, cluster_spec=backend_config)

    elif backend_name == "local" or backend_name is None:
        tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_BACKEND, "local")
        command_args = []
        command_separator = " "
        # If a docker_env attribute is defined in MLproject then it takes precedence over conda yaml
        # environments, so the project will be executed inside a docker container.
        if project.docker_env:
            tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_ENV,
                                            "docker")
            _validate_docker_env(project)
            _validate_docker_installation()
            image = _build_docker_image(work_dir=work_dir,
                                        repository_uri=project.name,
                                        base_image=project.docker_env.get('image'),
                                        run_id=active_run.info.run_id)
            command_args += _get_docker_command(image=image, active_run=active_run,
                                                docker_args=docker_args,
                                                volumes=project.docker_env.get("volumes"),
                                                user_env_vars=project.docker_env.get("environment"))
        # Synchronously create a conda environment (even though this may take some time)
        # to avoid failures due to multiple concurrent attempts to create the same conda env.
        elif use_conda:
            tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_ENV, "conda")
            command_separator = " && "
            conda_env_name = _get_or_create_conda_env(project.conda_env_path)
            command_args += _get_conda_command(conda_env_name)
        # In synchronous mode, run the entry point command in a blocking fashion, sending status
        # updates to the tracking server when finished. Note that the run state may not be
        # persisted to the tracking server if interrupted
        if synchronous:
            command_args += _get_entry_point_command(project, entry_point, parameters, storage_dir)
            command_str = command_separator.join(command_args)
            return _run_entry_point(command_str, work_dir, experiment_id,
                                    run_id=active_run.info.run_id)
        # Otherwise, invoke `mlflow run` in a subprocess
        return _invoke_mlflow_run_subprocess(
            work_dir=work_dir, entry_point=entry_point, parameters=parameters,
            experiment_id=experiment_id,
            use_conda=use_conda, storage_dir=storage_dir, run_id=active_run.info.run_id)
    elif backend_name == "kubernetes":
        from mlflow.projects import kubernetes as kb
        tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_ENV, "docker")
        tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_BACKEND,
                                        "kubernetes")
        _validate_docker_env(project)
        _validate_docker_installation()
        kube_config = _parse_kubernetes_config(backend_config)
        image = _build_docker_image(work_dir=work_dir,
                                    repository_uri=kube_config["repository-uri"],
                                    base_image=project.docker_env.get('image'),
                                    run_id=active_run.info.run_id)
        image_digest = kb.push_image_to_registry(image.tags[0])
        submitted_run = kb.run_kubernetes_job(
            project.name,
            active_run,
            image.tags[0],
            image_digest,
            _get_entry_point_command(project, entry_point, parameters, storage_dir),
            _get_run_env_vars(
                run_id=active_run.info.run_uuid,
                experiment_id=active_run.info.experiment_id
            ),
            kube_config.get('kube-context', None),
            kube_config['kube-job-template']
        )
        return submitted_run

    supported_backends = ["local", "databricks", "kubernetes"]
    raise ExecutionException("Got unsupported execution mode %s. Supported "
                             "values: %s" % (backend_name, supported_backends))


def run(uri, entry_point="main", version=None, parameters=None,
        docker_args=None, experiment_name=None, experiment_id=None,
        backend=None, backend_config=None, use_conda=True,
        storage_dir=None, synchronous=True, run_id=None):
    """
    Run an MLflow project. The project can be local or stored at a Git URI.

    MLflow provides built-in support for running projects locally or remotely on a Databricks or
    Kubernetes cluster. You can also run projects against other targets by installing an appropriate
    third-party plugin. See `Community Plugins <../plugins.html#community-plugins>`_ for more
    information.

    For information on using this method in chained workflows, see `Building Multistep Workflows
    <../projects.html#building-multistep-workflows>`_.

    :raises: :py:class:`mlflow.exceptions.ExecutionException` If a run launched in blocking mode
             is unsuccessful.

    :param uri: URI of project to run. A local filesystem path
                or a Git repository URI (e.g. https://github.com/mlflow/mlflow-example)
                pointing to a project directory containing an MLproject file.
    :param entry_point: Entry point to run within the project. If no entry point with the specified
                        name is found, runs the project file ``entry_point`` as a script,
                        using "python" to run ``.py`` files and the default shell (specified by
                        environment variable ``$SHELL``) to run ``.sh`` files.
    :param version: For Git-based projects, either a commit hash or a branch name.
    :param experiment_name: Name of experiment under which to launch the run.
    :param experiment_id: ID of experiment under which to launch the run.
    :param backend: Execution backend for the run: MLflow provides built-in support for "local",
                    "databricks", and "kubernetes" (experimental) backends. If running against
                    Databricks, will run against a Databricks workspace determined as follows:
                    if a Databricks tracking URI of the form ``databricks://profile`` has been set
                    (e.g. by setting the MLFLOW_TRACKING_URI environment variable), will run
                    against the workspace specified by <profile>. Otherwise, runs against the
                    workspace specified by the default Databricks CLI profile.
    :param backend_config: A dictionary, or a path to a JSON file (must end in '.json'), which will
                           be passed as config to the backend. The exact content which should be
                           provided is different for each execution backend and is documented
                           at https://www.mlflow.org/docs/latest/projects.html.
    :param use_conda: If True (the default), create a new Conda environment for the run and
                      install project dependencies within that environment. Otherwise, run the
                      project in the current environment without installing any project
                      dependencies.
    :param storage_dir: Used only if ``backend`` is "local". MLflow downloads artifacts from
                        distributed URIs passed to parameters of type ``path`` to subdirectories of
                        ``storage_dir``.
    :param synchronous: Whether to block while waiting for a run to complete. Defaults to True.
                        Note that if ``synchronous`` is False and ``backend`` is "local", this
                        method will return, but the current process will block when exiting until
                        the local run completes. If the current process is interrupted, any
                        asynchronous runs launched via this method will be terminated. If
                        ``synchronous`` is True and the run fails, the current process will
                        error out as well.
    :param run_id: Note: this argument is used internally by the MLflow project APIs and should
                   not be specified. If specified, the run ID will be used instead of
                   creating a new run.
    :return: :py:class:`mlflow.projects.SubmittedRun` exposing information (e.g. run ID)
             about the launched run.
    """

    cluster_spec_dict = backend_config
    if (backend_config and type(backend_config) != dict
            and os.path.splitext(backend_config)[-1] == ".json"):
        with open(backend_config, 'r') as handle:
            try:
                cluster_spec_dict = json.load(handle)
            except ValueError:
                _logger.error(
                    "Error when attempting to load and parse JSON cluster spec from file %s",
                    backend_config)
                raise

    if backend == "databricks":
        mlflow.projects.databricks.before_run_validations(mlflow.get_tracking_uri(), backend_config)
    elif backend == "local" and run_id is not None:
        backend_config[_MLFLOW_LOCAL_BACKEND_RUN_ID_CONFIG] = run_id

    experiment_id = _resolve_experiment_id(experiment_name=experiment_name,
                                           experiment_id=experiment_id)

    submitted_run_obj = _run(
        uri=uri, experiment_id=experiment_id, entry_point=entry_point, version=version,
        parameters=parameters, docker_args=docker_args, backend_name=backend,
        backend_config=cluster_spec_dict, use_conda=use_conda, storage_dir=storage_dir,
        synchronous=synchronous)
    if synchronous:
        _wait_for(submitted_run_obj)
    return submitted_run_obj


def _wait_for(submitted_run_obj):
    """Wait on the passed-in submitted run, reporting its status to the tracking server."""
    run_id = submitted_run_obj.run_id
    active_run = None
    # Note: there's a small chance we fail to report the run's status to the tracking server if
    # we're interrupted before we reach the try block below
    try:
        active_run = tracking.MlflowClient().get_run(run_id) if run_id is not None else None
        if submitted_run_obj.wait():
            _logger.info("=== Run (ID '%s') succeeded ===", run_id)
            _maybe_set_run_terminated(active_run, "FINISHED")
        else:
            _maybe_set_run_terminated(active_run, "FAILED")
            raise ExecutionException("Run (ID '%s') failed" % run_id)
    except KeyboardInterrupt:
        _logger.error("=== Run (ID '%s') interrupted, cancelling run ===", run_id)
        submitted_run_obj.cancel()
        _maybe_set_run_terminated(active_run, "FAILED")
        raise


def _get_conda_env_name(conda_env_path, env_id=None):
    conda_env_contents = open(conda_env_path).read() if conda_env_path else ""
    if env_id:
        conda_env_contents += env_id
    return "mlflow-%s" % hashlib.sha1(conda_env_contents.encode("utf-8")).hexdigest()


def _get_conda_bin_executable(executable_name):
    """
    Return path to the specified executable, assumed to be discoverable within the 'bin'
    subdirectory of a conda installation.

    The conda home directory (expected to contain a 'bin' subdirectory) is configurable via the
    ``mlflow.projects.MLFLOW_CONDA_HOME`` environment variable. If
    ``mlflow.projects.MLFLOW_CONDA_HOME`` is unspecified, this method simply returns the passed-in
    executable name.
    """
    conda_home = os.environ.get(MLFLOW_CONDA_HOME)
    if conda_home:
        return os.path.join(conda_home, "bin/%s" % executable_name)
    # Use CONDA_EXE as per https://github.com/conda/conda/issues/7126
    if "CONDA_EXE" in os.environ:
        conda_bin_dir = os.path.dirname(os.environ["CONDA_EXE"])
        return os.path.join(conda_bin_dir, executable_name)
    return executable_name


def _get_or_create_conda_env(conda_env_path, env_id=None):
    """
    Given a `Project`, creates a conda environment containing the project's dependencies if such a
    conda environment doesn't already exist. Returns the name of the conda environment.
    :param conda_env_path: Path to a conda yaml file.
    :param env_id: Optional string that is added to the contents of the yaml file before
                   calculating the hash. It can be used to distinguish environments that have the
                   same conda dependencies but are supposed to be different based on the context.
                   For example, when serving the model we may install additional dependencies to the
                   environment after the environment has been activated.
    """
    conda_path = _get_conda_bin_executable("conda")
    try:
        process.exec_cmd([conda_path, "--help"], throw_on_error=False)
    except EnvironmentError:
        raise ExecutionException("Could not find Conda executable at {0}. "
                                 "Ensure Conda is installed as per the instructions at "
                                 "https://conda.io/projects/conda/en/latest/"
                                 "user-guide/install/index.html. "
                                 "You can also configure MLflow to look for a specific "
                                 "Conda executable by setting the {1} environment variable "
                                 "to the path of the Conda executable"
                                 .format(conda_path, MLFLOW_CONDA_HOME))
    (_, stdout, _) = process.exec_cmd([conda_path, "env", "list", "--json"])
    env_names = [os.path.basename(env) for env in json.loads(stdout)['envs']]
    project_env_name = _get_conda_env_name(conda_env_path, env_id)
    if project_env_name not in env_names:
        _logger.info('=== Creating conda environment %s ===', project_env_name)
        if conda_env_path:
            process.exec_cmd([conda_path, "env", "create", "-n", project_env_name, "--file",
                              conda_env_path], stream_output=True)
        else:
            process.exec_cmd(
                [conda_path, "create", "-n", project_env_name, "python"], stream_output=True)
    return project_env_name


def _maybe_set_run_terminated(active_run, status):
    """
    If the passed-in active run is defined and still running (i.e. hasn't already been terminated
    within user code), mark it as terminated with the passed-in status.
    """
    if active_run is None:
        return
    run_id = active_run.info.run_id
    cur_status = tracking.MlflowClient().get_run(run_id).info.status
    if RunStatus.is_terminated(cur_status):
        return
    tracking.MlflowClient().set_terminated(run_id, status)


def _get_entry_point_command(project, entry_point, parameters, storage_dir):
    """
    Returns the shell command to execute in order to run the specified entry point.
    :param project: Project containing the target entry point
    :param entry_point: Entry point to run
    :param parameters: Parameters (dictionary) for the entry point command
    :param storage_dir: Base local directory to use for downloading remote artifacts passed to
                        arguments of type 'path'. If None, a temporary base directory is used.
    """
    storage_dir_for_run = _get_storage_dir(storage_dir)
    _logger.info(
        "=== Created directory %s for downloading remote URIs passed to arguments of"
        " type 'path' ===",
        storage_dir_for_run)
    commands = []
    commands.append(
        project.get_entry_point(entry_point).compute_command(parameters, storage_dir_for_run))
    return commands


def _run_entry_point(command, work_dir, experiment_id, run_id):
    """
    Run an entry point command in a subprocess, returning a SubmittedRun that can be used to
    query the run's status.
    :param command: Entry point command to run
    :param work_dir: Working directory in which to run the command
    :param run_id: MLflow run ID associated with the entry point execution.
    """
    env = os.environ.copy()
    env.update(_get_run_env_vars(run_id, experiment_id))
    _logger.info("=== Running command '%s' in run with ID '%s' === ", command, run_id)
    # in case os name is not 'nt', we are not running on windows. It introduces
    # bash command otherwise.
    if os.name != "nt":
        process = subprocess.Popen(["bash", "-c", command], close_fds=True, cwd=work_dir, env=env)
    else:
        # process = subprocess.Popen(command, close_fds=True, cwd=work_dir, env=env)
        process = subprocess.Popen(["cmd", "/c", command], close_fds=True, cwd=work_dir, env=env)
    return LocalSubmittedRun(run_id, process)


def _build_mlflow_run_cmd(
        uri, entry_point, storage_dir, use_conda, run_id, parameters):
    """
    Build and return an array containing an ``mlflow run`` command that can be invoked to locally
    run the project at the specified URI.
    """
    mlflow_run_arr = ["mlflow", "run", uri, "-e", entry_point, "--run-id", run_id]
    if storage_dir is not None:
        mlflow_run_arr.extend(["--storage-dir", storage_dir])
    if not use_conda:
        mlflow_run_arr.append("--no-conda")
    for key, value in parameters.items():
        mlflow_run_arr.extend(["-P", "%s=%s" % (key, value)])
    return mlflow_run_arr


def _run_mlflow_run_cmd(mlflow_run_arr, env_map):
    """
    Invoke ``mlflow run`` in a subprocess, which in turn runs the entry point in a child process.
    Returns a handle to the subprocess. Popen launched to invoke ``mlflow run``.
    """
    final_env = os.environ.copy()
    final_env.update(env_map)
    # Launch `mlflow run` command as the leader of its own process group so that we can do a
    # best-effort cleanup of all its descendant processes if needed
    if sys.platform == "win32":
        return subprocess.Popen(
            mlflow_run_arr, env=final_env, universal_newlines=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        return subprocess.Popen(
            mlflow_run_arr, env=final_env, universal_newlines=True, preexec_fn=os.setsid)


def _get_run_env_vars(run_id, experiment_id):
    """
    Returns a dictionary of environment variable key-value pairs to set in subprocess launched
    to run MLflow projects.
    """
    return {
        tracking._RUN_ID_ENV_VAR: run_id,
        tracking._TRACKING_URI_ENV_VAR: tracking.get_tracking_uri(),
        tracking._EXPERIMENT_ID_ENV_VAR: str(experiment_id),
    }


def _invoke_mlflow_run_subprocess(
        work_dir, entry_point, parameters, experiment_id, use_conda, storage_dir, run_id):
    """
    Run an MLflow project asynchronously by invoking ``mlflow run`` in a subprocess, returning
    a SubmittedRun that can be used to query run status.
    """
    _logger.info("=== Asynchronously launching MLflow run with ID %s ===", run_id)
    mlflow_run_arr = _build_mlflow_run_cmd(
        uri=work_dir, entry_point=entry_point, storage_dir=storage_dir, use_conda=use_conda,
        run_id=run_id, parameters=parameters)
    mlflow_run_subprocess = _run_mlflow_run_cmd(
        mlflow_run_arr, _get_run_env_vars(run_id, experiment_id))
    return LocalSubmittedRun(run_id, mlflow_run_subprocess)


def _get_conda_command(conda_env_name):
    #  Checking for newer conda versions
    if os.name != 'nt' and ('CONDA_EXE' in os.environ or 'MLFLOW_CONDA_HOME' in os.environ):
        conda_path = _get_conda_bin_executable("conda")
        activate_conda_env = [
            'source {}/../etc/profile.d/conda.sh'.format(os.path.dirname(conda_path))
        ]
        activate_conda_env += ["conda activate {} 1>&2".format(conda_env_name)]
    else:
        activate_path = _get_conda_bin_executable("activate")
        # in case os name is not 'nt', we are not running on windows. It introduces
        # bash command otherwise.
        if os.name != "nt":
            return ["source %s %s 1>&2" % (activate_path, conda_env_name)]
        else:
            return ["conda activate %s" % (conda_env_name)]
    return activate_conda_env


def _validate_execution_environment(project, backend):
    if project.docker_env and backend == "databricks":
        raise ExecutionException(
            "Running docker-based projects on Databricks is not yet supported.")


def _get_local_uri_or_none(uri):
    if uri == "databricks":
        return None, None
    parsed_uri = urllib.parse.urlparse(uri)
    if not parsed_uri.netloc and parsed_uri.scheme in ("", "file", "sqlite"):
        path = urllib.request.url2pathname(parsed_uri.path)
        if parsed_uri.scheme == "sqlite":
            uri = path_to_local_sqlite_uri(_MLFLOW_DOCKER_TRACKING_DIR_PATH)
        else:
            uri = path_to_local_file_uri(_MLFLOW_DOCKER_TRACKING_DIR_PATH)
        return path, uri
    else:
        return None, None


def _get_docker_command(image, active_run, docker_args=None, volumes=None, user_env_vars=None):
    docker_path = "docker"
    cmd = [docker_path, "run", "--rm"]

    if docker_args:
        for key, value in docker_args.items():
            cmd += ['--' + key, value]

    env_vars = _get_run_env_vars(run_id=active_run.info.run_id,
                                 experiment_id=active_run.info.experiment_id)
    tracking_uri = tracking.get_tracking_uri()
    tracking_cmds, tracking_envs = _get_docker_tracking_cmd_and_envs(tracking_uri)
    artifact_cmds, artifact_envs = \
        _get_docker_artifact_storage_cmd_and_envs(active_run.info.artifact_uri)

    cmd += tracking_cmds + artifact_cmds
    env_vars.update(tracking_envs)
    env_vars.update(artifact_envs)
    if user_env_vars is not None:
        for user_entry in user_env_vars:
            if isinstance(user_entry, list):
                # User has defined a new environment variable for the docker environment
                env_vars[user_entry[0]] = user_entry[1]
            else:
                # User wants to copy an environment variable from system environment
                system_var = os.environ.get(user_entry)
                if system_var is None:
                    raise MlflowException(
                        "This project expects the %s environment variables to "
                        "be set on the machine running the project, but %s was "
                        "not set. Please ensure all expected environment variables "
                        "are set" % (", ".join(user_env_vars), user_entry))
                env_vars[user_entry] = system_var

    if volumes is not None:
        for v in volumes:
            cmd += ["-v", v]

    for key, value in env_vars.items():
        cmd += ["-e", "{key}={value}".format(key=key, value=value)]
    cmd += [image.tags[0]]
    return cmd


def _validate_docker_installation():
    """
    Verify if Docker is installed on host machine.
    """
    try:
        docker_path = "docker"
        process.exec_cmd([docker_path, "--help"], throw_on_error=False)
    except EnvironmentError:
        raise ExecutionException("Could not find Docker executable. "
                                 "Ensure Docker is installed as per the instructions "
                                 "at https://docs.docker.com/install/overview/.")


def _validate_docker_env(project):
    if not project.name:
        raise ExecutionException("Project name in MLProject must be specified when using docker "
                                 "for image tagging.")
    if not project.docker_env.get('image'):
        raise ExecutionException("Project with docker environment must specify the docker image "
                                 "to use via an 'image' field under the 'docker_env' field.")


def _parse_kubernetes_config(backend_config):
    """
    Creates build context tarfile containing Dockerfile and project code, returning path to tarfile
    """
    if not backend_config:
        raise ExecutionException("Backend_config file not found.")
    kube_config = backend_config.copy()
    if 'kube-job-template-path' not in backend_config.keys():
        raise ExecutionException("'kube-job-template-path' attribute must be specified in "
                                 "backend_config.")
    kube_job_template = backend_config['kube-job-template-path']
    if os.path.exists(kube_job_template):
        with open(kube_job_template, 'r') as job_template:
            yaml_obj = yaml.safe_load(job_template.read())
        kube_job_template = yaml_obj
        kube_config['kube-job-template'] = kube_job_template
    else:
        raise ExecutionException("Could not find 'kube-job-template-path': {}".format(
            kube_job_template))
    if 'kube-context' not in backend_config.keys():
        _logger.debug("Could not find kube-context in backend_config."
                      " Using current context or in-cluster config.")
    if 'repository-uri' not in backend_config.keys():
        raise ExecutionException("Could not find 'repository-uri' in backend_config.")
    return kube_config


def _create_docker_build_ctx(work_dir, dockerfile_contents):
    """
    Creates build context tarfile containing Dockerfile and project code, returning path to tarfile
    """
    directory = tempfile.mkdtemp()
    try:
        dst_path = os.path.join(directory, "mlflow-project-contents")
        shutil.copytree(src=work_dir, dst=dst_path)
        with open(os.path.join(dst_path, _GENERATED_DOCKERFILE_NAME), "w") as handle:
            handle.write(dockerfile_contents)
        _, result_path = tempfile.mkstemp()
        file_utils.make_tarfile(
            output_filename=result_path,
            source_dir=dst_path, archive_name=_PROJECT_TAR_ARCHIVE_NAME)
    finally:
        shutil.rmtree(directory)
    return result_path


def _build_docker_image(work_dir, repository_uri, base_image, run_id):
    """
    Build a docker image containing the project in `work_dir`, using the base image.
    """
    image_uri = _get_docker_image_uri(repository_uri=repository_uri, work_dir=work_dir)
    dockerfile = (
        "FROM {imagename}\n"
        "COPY {build_context_path}/ {workdir}\n"
        "WORKDIR {workdir}\n"
    ).format(imagename=base_image,
             build_context_path=_PROJECT_TAR_ARCHIVE_NAME,
             workdir=_MLFLOW_DOCKER_WORKDIR_PATH)
    build_ctx_path = _create_docker_build_ctx(work_dir, dockerfile)
    with open(build_ctx_path, 'rb') as docker_build_ctx:
        _logger.info("=== Building docker image %s ===", image_uri)
        client = docker.from_env()
        image, _ = client.images.build(
            tag=image_uri, forcerm=True,
            dockerfile=posixpath.join(_PROJECT_TAR_ARCHIVE_NAME, _GENERATED_DOCKERFILE_NAME),
            fileobj=docker_build_ctx, custom_context=True, encoding="gzip")
    try:
        os.remove(build_ctx_path)
    except Exception:  # pylint: disable=broad-except
        _logger.info("Temporary docker context file %s was not deleted.", build_ctx_path)
    tracking.MlflowClient().set_tag(run_id,
                                    MLFLOW_DOCKER_IMAGE_URI,
                                    image_uri)
    tracking.MlflowClient().set_tag(run_id,
                                    MLFLOW_DOCKER_IMAGE_ID,
                                    image.id)
    return image


def _get_docker_image_uri(repository_uri, work_dir):
    """
    Returns an appropriate Docker image URI for a project based on the git hash of the specified
    working directory.

    :param repository_uri: The URI of the Docker repository with which to tag the image. The
                           repository URI is used as the prefix of the image URI.
    :param work_dir: Path to the working directory in which to search for a git commit hash
    """
    repository_uri = repository_uri if repository_uri else "docker-project"
    # Optionally include first 7 digits of git SHA in tag name, if available.
    git_commit = _get_git_commit(work_dir)
    version_string = ":" + git_commit[:7] if git_commit else ""
    return repository_uri + version_string


def _get_local_artifact_cmd_and_envs(artifact_repo):
    artifact_dir = artifact_repo.artifact_dir
    container_path = artifact_dir
    if not os.path.isabs(container_path):
        container_path = os.path.join(_MLFLOW_DOCKER_WORKDIR_PATH, container_path)
        container_path = os.path.normpath(container_path)
    abs_artifact_dir = os.path.abspath(artifact_dir)
    return ["-v", "%s:%s" % (abs_artifact_dir, container_path)], {}


def _get_s3_artifact_cmd_and_envs(artifact_repo):
    # pylint: disable=unused-argument
    if platform.system() == "Windows":
        win_user_dir = os.environ["USERPROFILE"]
        aws_path = os.path.join(win_user_dir, ".aws")
    else:
        aws_path = posixpath.expanduser("~/.aws")

    volumes = []
    if posixpath.exists(aws_path):
        volumes = ["-v", "%s:%s" % (str(aws_path), "/.aws")]
    envs = {
        "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID"),
        "MLFLOW_S3_ENDPOINT_URL": os.environ.get("MLFLOW_S3_ENDPOINT_URL")
    }
    envs = dict((k, v) for k, v in envs.items() if v is not None)
    return volumes, envs


def _get_azure_blob_artifact_cmd_and_envs(artifact_repo):
    # pylint: disable=unused-argument
    envs = {
        "AZURE_STORAGE_CONNECTION_STRING": os.environ.get("AZURE_STORAGE_CONNECTION_STRING"),
        "AZURE_STORAGE_ACCESS_KEY": os.environ.get("AZURE_STORAGE_ACCESS_KEY")
    }
    envs = dict((k, v) for k, v in envs.items() if v is not None)
    return [], envs


def _get_gcs_artifact_cmd_and_envs(artifact_repo):
    # pylint: disable=unused-argument
    cmds = []
    envs = {}

    if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
        credentials_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        cmds = ["-v", "{}:/.gcs".format(credentials_path)]
        envs["GOOGLE_APPLICATION_CREDENTIALS"] = "/.gcs"
    return cmds, envs


def _get_hdfs_artifact_cmd_and_envs(artifact_repo):
    # pylint: disable=unused-argument
    cmds = []
    envs = {
        "MLFLOW_KERBEROS_TICKET_CACHE": os.environ.get("MLFLOW_KERBEROS_TICKET_CACHE"),
        "MLFLOW_KERBEROS_USER": os.environ.get("MLFLOW_KERBEROS_USER"),
        "MLFLOW_PYARROW_EXTRA_CONF": os.environ.get("MLFLOW_PYARROW_EXTRA_CONF")
    }
    envs = dict((k, v) for k, v in envs.items() if v is not None)

    if "MLFLOW_KERBEROS_TICKET_CACHE" in envs:
        ticket_cache = envs["MLFLOW_KERBEROS_TICKET_CACHE"]
        cmds = ["-v", "{}:{}".format(ticket_cache, ticket_cache)]
    return cmds, envs


_artifact_storages = {
    LocalArtifactRepository: _get_local_artifact_cmd_and_envs,
    S3ArtifactRepository: _get_s3_artifact_cmd_and_envs,
    AzureBlobArtifactRepository: _get_azure_blob_artifact_cmd_and_envs,
    HdfsArtifactRepository: _get_hdfs_artifact_cmd_and_envs,
    GCSArtifactRepository: _get_gcs_artifact_cmd_and_envs,
}


def _get_docker_artifact_storage_cmd_and_envs(artifact_uri):
    artifact_repo = get_artifact_repository(artifact_uri)
    _get_cmd_and_envs = _artifact_storages.get(type(artifact_repo))
    if _get_cmd_and_envs is not None:
        return _get_cmd_and_envs(artifact_repo)
    else:
        return [], {}


def _get_docker_tracking_cmd_and_envs(tracking_uri):
    cmds = []
    env_vars = dict()

    local_path, container_tracking_uri = _get_local_uri_or_none(tracking_uri)
    if local_path is not None:
        cmds = ["-v", "%s:%s" % (local_path, _MLFLOW_DOCKER_TRACKING_DIR_PATH)]
        env_vars[tracking._TRACKING_URI_ENV_VAR] = container_tracking_uri
    if is_databricks_uri(tracking_uri):
        db_profile = get_db_profile_from_uri(tracking_uri)
        config = databricks_utils.get_databricks_host_creds(db_profile)
        # We set these via environment variables so that only the current profile is exposed, rather
        # than all profiles in ~/.databrickscfg; maybe better would be to mount the necessary
        # part of ~/.databrickscfg into the container
        env_vars[tracking._TRACKING_URI_ENV_VAR] = 'databricks'
        env_vars['DATABRICKS_HOST'] = config.host
        if config.username:
            env_vars['DATABRICKS_USERNAME'] = config.username
        if config.password:
            env_vars['DATABRICKS_PASSWORD'] = config.password
        if config.token:
            env_vars['DATABRICKS_TOKEN'] = config.token
        if config.ignore_tls_verification:
            env_vars['DATABRICKS_INSECURE'] = config.ignore_tls_verification
    return cmds, env_vars


__all__ = [
    "run",
    "SubmittedRun"
]
