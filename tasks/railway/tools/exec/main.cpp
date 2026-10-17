// railexec - runs one command and reports how it ended. Linux only.
//
//   railexec <report-fd> <command> [args...]
//
// Meant to run inside the evaluation sandbox, where the orchestrator cannot
// observe the measured process directly. Writes one line to <report-fd>:
//   exit <code> signal <sig> maxrss_kb <kb> user_s <s> sys_s <s>
// The report fd is close-on-exec, so the measured program cannot write to it,
// and the child is killed if railexec dies.
#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>

int main(int argc, char** argv) {
  if (argc < 3) {
    std::fprintf(stderr, "usage: railexec <report-fd> <command> [args...]\n");
    return 2;
  }
  const int report = std::atoi(argv[1]);
  if (fcntl(report, F_SETFD, FD_CLOEXEC) != 0) {
    std::perror("railexec: report fd");
    return 2;
  }
  const pid_t parent = getpid();
  const pid_t pid = fork();
  if (pid < 0) {
    std::perror("railexec: fork");
    return 2;
  }
  if (pid == 0) {
    prctl(PR_SET_PDEATHSIG, SIGKILL);
    if (getppid() != parent) _exit(127);
    execvp(argv[2], argv + 2);
    std::perror("railexec: exec");
    _exit(127);
  }
  int status = 0;
  rusage ru{};
  while (wait4(pid, &status, 0, &ru) < 0) {
  }
  const int code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
  const int sig = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
  dprintf(report, "exit %d signal %d maxrss_kb %ld user_s %.3f sys_s %.3f\n", code, sig, ru.ru_maxrss,
          double(ru.ru_utime.tv_sec) + double(ru.ru_utime.tv_usec) / 1e6,
          double(ru.ru_stime.tv_sec) + double(ru.ru_stime.tv_usec) / 1e6);
  close(report);
  return 0;
}
