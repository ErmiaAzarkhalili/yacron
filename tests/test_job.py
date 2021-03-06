import yacron.job
import yacron.config
import asyncio
import pytest
import aiosmtplib
import raven
from unittest.mock import Mock, patch


@pytest.mark.parametrize("save_limit, output", [
    (10, 'line1\nline2\nline3\nline4\n'),
    (1, '   [.... 3 lines discarded ...]\nline4\n'),
    (2, 'line1\n   [.... 2 lines discarded ...]\nline4\n'),
])
def test_stream_reader(save_limit, output):
    loop = asyncio.get_event_loop()
    fake_stream = asyncio.StreamReader()
    reader = yacron.job.StreamReader("cronjob-1", "stderr", fake_stream,
                                     save_limit)

    async def producer(fake_stream):
        fake_stream.feed_data(b"line1\nline2\nline3\nline4\n")
        fake_stream.feed_eof()

    _, out = loop.run_until_complete(asyncio.gather(
        producer(fake_stream),
        reader.join()))

    assert out == output


A_JOB = '''
jobs:
  - name: test
    command: ls
    schedule: "* * * * *"
    onSuccess:
      report:
        mail:
          from: example@foo.com
          to: example@bar.com
          smtpHost: smtp1
          smtpPort: 1025
'''


@pytest.mark.parametrize("report_type, stdout, stderr, subject, body", [
    (yacron.job.ReportType.SUCCESS, "out", "err",
     "Cron job 'test' completed",
     'STDOUT:\n---\nout\n---\nSTDERR:\nerr'),

    (yacron.job.ReportType.FAILURE, "out", "err",
     "Cron job 'test' failed",
     'STDOUT:\n---\nout\n---\nSTDERR:\nerr'),

    (yacron.job.ReportType.FAILURE, None, None,
     "Cron job 'test' failed",
     "(no output was captured)"),

    (yacron.job.ReportType.FAILURE, None, "err",
     "Cron job 'test' failed",
     'err'),

    (yacron.job.ReportType.FAILURE, "out", None,
     "Cron job 'test' failed",
     'out'),
])
def test_report_mail(report_type, stdout, stderr, subject, body):
    job_config = yacron.config.parse_config_string(A_JOB)[0]
    job = Mock(config=job_config, stdout=stdout, stderr=stderr)
    mail = yacron.job.MailReporter()
    loop = asyncio.get_event_loop()

    connect_calls = []
    messages_sent = []

    async def connect(self):
        connect_calls.append(self)

    async def send_message(self, message):
        messages_sent.append(message)

    real_init = aiosmtplib.SMTP.__init__
    smtp_init_args = None
    def init(self, *args, **kwargs):
        nonlocal smtp_init_args
        smtp_init_args = args, kwargs
        real_init(self, *args, **kwargs)

    with patch("aiosmtplib.SMTP.__init__", init), \
         patch("aiosmtplib.SMTP.connect", connect), \
         patch("aiosmtplib.SMTP.send_message", send_message):
        loop.run_until_complete(mail.report(report_type,
                                            job,
                                            job_config.onSuccess['report']))

    assert smtp_init_args == ((), {'hostname': 'smtp1', 'port': 1025})
    assert len(connect_calls) == 1
    assert len(messages_sent) == 1
    message = messages_sent[0]
    assert message['From'] == "example@foo.com"
    assert message['To'] == "example@bar.com"
    assert message['Subject'] == subject
    assert message.get_payload() == body


@pytest.mark.parametrize("report_type, dsn_from, body, extra, expected_dsn", [
    (yacron.job.ReportType.SUCCESS,
     "value",
     "Cron job 'test' completed\n\nSTDOUT:\n---\nout\n---\nSTDERR:\nerr",
     {
        'job': 'test',
        'exit_code': 0,
        'command': 'ls',
        'shell': '/bin/sh',
        'success': True,
    }, "http://xxx:yyy@sentry/1"),
    (yacron.job.ReportType.FAILURE,
     "file",
     "Cron job 'test' failed\n\nSTDOUT:\n---\nout\n---\nSTDERR:\nerr",
     {
        'job': 'test',
        'exit_code': 0,
        'command': 'ls',
        'shell': '/bin/sh',
        'success': False,
    }, "http://xxx:yyy@sentry/2"),
    (yacron.job.ReportType.FAILURE,
     "envvar",
     "Cron job 'test' failed\n\nSTDOUT:\n---\nout\n---\nSTDERR:\nerr",
     {
        'job': 'test',
        'exit_code': 0,
        'command': 'ls',
        'shell': '/bin/sh',
        'success': False,
    }, "http://xxx:yyy@sentry/3"),
])
def test_report_sentry(report_type, dsn_from, body, extra, expected_dsn,
                       tmpdir, monkeypatch):
    job_config = yacron.config.parse_config_string(A_JOB)[0]

    p = tmpdir.join("sentry-secret-dsn")
    p.write("http://xxx:yyy@sentry/2")

    monkeypatch.setenv("TEST_SENTRY_DSN", "http://xxx:yyy@sentry/3")

    if dsn_from == 'value':
        job_config.onSuccess['report']['sentry'] = {
            "dsn": {
                "value": "http://xxx:yyy@sentry/1",
                "fromFile": None,
                "fromEnvVar": None,
            }
        }
    elif dsn_from == 'file':
        job_config.onSuccess['report']['sentry'] = {
            "dsn": {
                "value": None,
                "fromFile": str(p),
                "fromEnvVar": None,
            }
        }
    elif dsn_from == 'envvar':
        job_config.onSuccess['report']['sentry'] = {
            "dsn": {
                "value": None,
                "fromFile": None,
                "fromEnvVar": "TEST_SENTRY_DSN",
            }
        }
    else:
        raise AssertionError

    job = Mock(config=job_config, stdout="out", stderr="err", retcode=0)
    loop = asyncio.get_event_loop()

    messages_sent = []

    def captureMessage(self, body, extra):
        messages_sent.append((body, extra))

    real_init = raven.Client.__init__
    init_args = (), {}

    def init(self, *args, **kwargs):
        nonlocal init_args
        init_args = args, kwargs
        real_init(self, *args, **kwargs)

    sentry = yacron.job.SentryReporter()
    with patch("raven.Client.__init__", init), \
         patch("raven.Client.captureMessage", captureMessage):
        loop.run_until_complete(sentry.report(report_type,
                                              job,
                                              job_config.onSuccess['report']))

    args, kwargs = init_args
    assert kwargs.get('dsn') == expected_dsn

    assert len(messages_sent) == 1
    got_body, got_extra = messages_sent[0]
    assert got_body == body
    assert got_extra == extra


@pytest.mark.parametrize("shell, command, expected_type, expected_args", [
    ('', "Civ 6", 'shell', ('Civ 6',)),
    ('', ["echo", "hello"], 'exec', ('echo', 'hello')),
    ('bash', 'echo "hello"', 'exec', ('bash', '-c', 'echo "hello"',)),
])
def test_job_run(monkeypatch, shell, command, expected_type, expected_args):

    shell_commands = []
    exec_commands = []

    async def create_subprocess_common(*args, **kwargs):
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stdout.feed_data(b"out\n")
        stdout.feed_eof()
        stderr.feed_data(b"err\n")
        stderr.feed_eof()
        proc = Mock(stdout=stdout, stderr=stderr)

        async def wait():
            return

        proc.wait = wait
        return proc

    async def create_subprocess_shell(*args, **kwargs):
        shell_commands.append((args, kwargs))
        return await create_subprocess_common(*args, **kwargs)

    async def create_subprocess_exec(*args, **kwargs):
        exec_commands.append((args, kwargs))
        return await create_subprocess_common(*args, **kwargs)

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        create_subprocess_exec)
    monkeypatch.setattr("asyncio.create_subprocess_shell",
                        create_subprocess_shell)

    if isinstance(command, list):
        command_snippet = '\n'.join(
            ["    command:"] + ['      - ' + arg for arg in command])
    else:
        command_snippet = '    command: ' + command

    job_config = yacron.config.parse_config_string('''
jobs:
  - name: test
{command}
    schedule: "* * * * *"
    shell: {shell}
    captureStderr: true
    captureStdout: true
    environment:
      - key: FOO
        value: bar
'''.format(command=command_snippet, shell=shell))[0]

    job = yacron.job.RunningJob(job_config, None)

    async def run(job):
        await job.start()
        await job.wait()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(job))

    if shell_commands:
        run_type = 'shell'
        assert len(shell_commands) == 1
        args, kwargs = shell_commands[0]
    elif exec_commands:
        run_type = 'exec'
        assert len(exec_commands) == 1
        args, kwargs = exec_commands[0]
    else:
        raise AssertionError

    assert kwargs['env']['FOO'] == 'bar'
    assert run_type == expected_type
    assert args == expected_args


def test_execution_timeout():
    job_config = yacron.config.parse_config_string('''
jobs:
  - name: test
    command: |
        echo "hello"
        sleep 1
        echo "world"
    executionTimeout: 0.25
    schedule: "* * * * *"
    captureStderr: false
    captureStdout: true
''')[0]

    async def test(job):
        await job.start()
        await job.wait()
        return job.stdout

    job = yacron.job.RunningJob(job_config, None)
    loop = asyncio.get_event_loop()
    stdout = loop.run_until_complete(test(job))
    assert stdout == "hello\n"


def test_error1():
    job_config = yacron.config.parse_config_string('''
jobs:
  - name: test
    command: echo "hello"
    schedule: "* * * * *"
''')[0]
    job = yacron.job.RunningJob(job_config, None)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(job.start())
    with pytest.raises(RuntimeError):
        loop.run_until_complete(job.start())


def test_error2():
    job_config = yacron.config.parse_config_string('''
jobs:
  - name: test
    command: echo "hello"
    schedule: "* * * * *"
''')[0]
    job = yacron.job.RunningJob(job_config, None)

    loop = asyncio.get_event_loop()
    with pytest.raises(RuntimeError):
        loop.run_until_complete(job.wait())


def test_error3():
    job_config = yacron.config.parse_config_string('''
jobs:
  - name: test
    command: echo "hello"
    schedule: "* * * * *"
''')[0]
    job = yacron.job.RunningJob(job_config, None)

    loop = asyncio.get_event_loop()
    with pytest.raises(RuntimeError):
        loop.run_until_complete(job.cancel())
