# Common Log Format Regex Patterns

Load this file into context when dealing with specialized log formats like syslog, Apache, Nginx, Docker, Kubernetes, pytest, Gradle, or CMake.

## Table of Contents

1. [Syslog (RFC 3164)](#1-syslog-rfc-3164)
2. [Syslog (RFC 5424)](#2-syslog-rfc-5424)
3. [Apache / Nginx Access Logs](#3-apache--nginx-access-logs)
4. [Docker Container Logs](#4-docker-container-logs)
5. [Kubernetes (kubectl / pod logs)](#5-kubernetes-kubectl--pod-logs)
6. [pytest Output](#6-pytest-output)
7. [Gradle Build Log](#7-gradle-build-log)
8. [CMake Output](#8-cmake-output)
9. [Java / JVM Stack Traces](#9-java--jvm-stack-traces)
10. [CI Pipeline Logs (GitHub Actions / GitLab CI)](#10-ci-pipeline-logs)

---

## 1. Syslog (RFC 3164)

```text
<priority>?timestamp hostname process[pid]: message
May 24 10:15:30 webserver sshd[12345]: Failed password for root from 10.0.0.1 port 22 ssh2
```

**Regex:**

```python
import re

RFC3164 = re.compile(
    r'^(?P<timestamp>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+'
    r'(?P<hostname>\S+)\s+'
    r'(?P<process>\S+)\[(?P<pid>\d+)\]:\s+'
    r'(?P<message>.+)$'
)
```

## 2. Syslog (RFC 5424)

```text
<priority>version timestamp hostname appname procid msgid structured-data message
<13>1 2026-05-24T10:15:30.123Z webserver sshd 12345 - [exampleSDID@32473 iut="3"] Failed password
```

**Regex:**

```python
RFC5424 = re.compile(
    r'^<\d+>\d+\s+'
    r'(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)\s+'
    r'(?P<hostname>\S+)\s+'
    r'(?P<appname>\S+)\s+'
    r'(?P<procid>\S+)\s+'
    r'(?P<msgid>\S+)\s+'
    r'(?P<structured>\[.*?\])\s+'
    r'(?P<message>.*)$'
)
```

## 3. Apache / Nginx Access Logs

### Combined Log Format

```text
127.0.0.1 - frank [10/Oct/2026:13:55:36 +0800] "GET /api/users HTTP/1.1" 200 2326 "https://example.com/" "Mozilla/5.0"
```

**Regex:**

```python
COMMON_LOG_FORMAT = re.compile(
    r'^(?P<ip>\S+)\s+'
    r'(?P<ident>\S+)\s+'
    r'(?P<auth>\S+)\s+'
    r'\[(?P<timestamp>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+(?P<protocol>[^"]+)"\s+'
    r'(?P<status>\d{3})\s+'
    r'(?P<bytes>\d+)\s+'
    r'"(?P<referer>[^"]*)"\s+'
    r'"(?P<agent>[^"]*)"'
)
```

### Error Log

```text
[Wed May 24 10:15:30.123456 2026] [core:error] [pid 12345:tid 12345] [client 10.0.0.1:12345] File does not exist: /var/www/html/404.html
```

**Regex:**

```python
APACHE_ERROR = re.compile(
    r'^\[(?P<timestamp>[^\]]+)\]\s+'
    r'\[(?P<module>[^:]+):(?P<level>\w+)\]\s+'
    r'\[pid\s+(?P<pid>\d+)(?::tid\s+\d+)?\]\s+'
    r'(?:\[client\s+(?P<client>[^\]]+)\]\s+)?'
    r'(?P<message>.*)$'
)
```

## 4. Docker Container Logs

### Default JSON driver

```json
{"log":"2026-05-24T10:15:30.123Z ERROR: Connection refused\n","stream":"stdout","time":"2026-05-24T10:15:30.123456789Z"}
```

Parse as JSON-lines, then extract `log` field.

### docker-compose output

```text
web_1    | 2026-05-24T10:15:30.123Z INFO  Server started on port 8080
db_1     | 2026-05-24T10:15:30.456Z ERROR connection refused
```

**Regex:**

```python
DOCKER_COMPOSE = re.compile(
    r'^(?P<service>\S+)\s+\|\s+'
    r'(?P<message>.*)$'
)
```

## 5. Kubernetes (kubectl / pod logs)

```text
2026-05-24T10:15:30.123456789Z stdout INFO  Server started on port 8080
2026-05-24T10:15:30.456789012Z stderr ERROR Failed to connect to database
```

**Regex:**

```python
K8S_LOG = re.compile(
    r'^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+'
    r'(?P<stream>stdout|stderr)\s+'
    r'(?P<severity>[A-Z]+)\s+'
    r'(?P<message>.*)$'
)
```

### Kubernetes Events

```text
30s         Warning   Unhealthy   pod/my-app-7d4f8b9c6-x2k3l   Liveness probe failed: Get "http://10.0.0.1:8080/health": dial tcp 10.0.0.1:8080: connect: connection refused
```

**Regex:**

```python
K8S_EVENT = re.compile(
    r'^(?P<age>[\dhms]+)\s+'
    r'(?P<type>Normal|Warning)\s+'
    r'(?P<reason>\S+)\s+'
    r'(?P<object>\S+\/\S+)\s+'
    r'(?P<message>.*)$'
)
```

## 6. pytest Output

```text
============================= test session starts ==============================
platform linux -- Python 3.11.0, pytest-7.4.0, pluggy-1.3.0
rootdir: /home/user/project
collected 42 items

tests/test_auth.py ..F.                                              [ 9%]
tests/test_api.py .............                                      [ 40%]
tests/test_db.py .................E.                                 [ 81%]
tests/test_ui.py ........                                            [100%]

=================================== FAILURES ===================================
_______________________________ test_login_fail _______________________________

    def test_login_fail():
>       assert login("admin", "wrong") == "success"
E       AssertionError: assert 'invalid credentials' == 'success'

=========================== short test summary info ============================
FAILED tests/test_auth.py::test_login_fail - AssertionError
ERROR  tests/test_db.py::test_db_connection - ConnectionError: database timeout
==================== 1 failed, 1 error, 40 passed in 2.34s ====================
```

**Key patterns:**

- `PASSED`, `FAILED`, `ERROR`, `SKIPPED`, `XFail` per test
- `FAILURES` / `ERRORS` section headers
- Line like `FAILED path::test_name - reason`
- Summary line: `X failed, Y error, Z passed in ...`

```python
PYTEST_RESULT = re.compile(
    r'^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XPASS|XFAIL)\s+'
    r'(?P<test>.*)$'
)

PYTEST_SUMMARY_LINE = re.compile(
    r'^(?P<failed>\d+)\s+failed'
    r'(?:,\s+(?P<errors>\d+)\s+error)?'
    r'(?:,\s+(?P<passed>\d+)\s+passed)?'
)
```

## 7. Gradle Build Log

```text
> Task :compileKotlin
> Task :processResources
> Task :classes UP-TO-DATE
> Task :jar
> Task :test FAILED
```

**Regex:**

```python
GRADLE_TASK = re.compile(
    r'^>\s+Task\s+(?P<task>:\S+)\s*(?P<status>FAILED|UP-TO-DATE|SKIPPED|FROM-CACHE)?$'
)
```

Build failure output:

```text
* What went wrong:
Execution failed for task ':test'.
> There were failing tests. See the results at: file:///home/user/build/reports/tests/test/index.html
```

```python
GRADLE_ERROR = re.compile(
    r'^>\s+(?P<message>.+)$'
)
```

## 8. CMake Output

```text
-- Configuring done
-- Generating done
-- Build files have been written to: /home/user/build
CMake Error at CMakeLists.txt:15 (find_package):
  By not providing "FindOpenSSL.cmake" in CMAKE_MODULE_PATH this project
  has asked CMake to find a package configuration file provided by
  "OpenSSL", but CMake did not find one.
```

**Regex:**

```python
CMAKE_STATUS = re.compile(
    r'^--\s+(?P<message>.+)$'
)

CMAKE_ERROR = re.compile(
    r'^CMake\s+(?P<level>Error|Warning)\s+at\s+'
    r'(?P<file>[^:]+):(?P<line>\d+)\s*\((?P<function>[^)]+)\):\s*'
    r'(?P<message>.*)$'
)
```

## 9. Java / JVM Stack Traces

```text
java.lang.NullPointerException: Cannot invoke "String.length()" because "name" is null
    at com.example.UserService.getName(UserService.java:42)
    at com.example.UserController.getUser(UserController.java:18)
    at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
    ... 3 common frames omitted
Caused by: java.sql.SQLException: Connection refused
    at com.example.db.DatabaseConnection.connect(DatabaseConnection.java:55)
    ... 8 more
```

**Parsing strategy:** Group multi-line traces into single events. Each trace starts with `Exception class: message` and continues with indented `at ...` lines.

```python
STACK_TRACE_START = re.compile(
    r'^(?P<exception>\S+(?:\.\S+)*(?:Exception|Error|Fault|Throwable))'
    r'(?::\s*(?P<message>.*))?$'
)

STACK_TRACE_FRAME = re.compile(
    r'^\s+at\s+(?P<class>\S+)\.(?P<method>\S+)\((?P<file>[^:]+)(?::(?P<line>\d+))?\)$'
)

STACK_CAUSED_BY = re.compile(
    r'^Caused by:\s+(?P<exception>\S+)(?::\s*(?P<message>.*))?$'
)

STACK_MORE_FRAMES = re.compile(
    r'^\s+\.\.\.\s+\d+\s+common\s+frames\s+omitted$'
)
```

## 10. CI Pipeline Logs

### GitHub Actions

```text
2026-05-24T10:15:30.1234567Z ##[section]Starting: Build
2026-05-24T10:15:31.2345678Z ##[command]docker build -t myapp:latest .
2026-05-24T10:15:35.5678901Z ##[error]Process completed with exit code 1.
```

**Regex:**

```python
GH_ACTIONS = re.compile(
    r'^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+'
    r'##\[(?P<level>section|command|error|warning|group|endgroup|debug)]'
    r'(?P<message>.*)$'
)
```

### GitLab CI

```text
Section completed: build_script
Uploading artifacts for successful job
Job succeeded
```

```text
Running with gitlab-runner 15.0.0 (xxx)
  on runner-xxx
Preparing the "docker" executor
Using Docker executor with image node:18-alpine ...
Job succeeded
```
