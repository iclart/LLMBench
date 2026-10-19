// Manual compiler-runtime preflight, not an engine test. Build with the same
// MinGW POSIX toolchain and run repeatedly. A heap-corruption exit reproduces
// the emutls/C++ TLS destructor ordering defect (MSYS2 issue 2519).
// The benchmark avoids worker TLS destruction on Windows and exits only after
// every worker completed and every output file was closed.
#include <atomic>
#include <thread>
#include <vector>
struct Scratch { std::vector<int> a,b,c,d; };
Scratch& scratch(){thread_local Scratch value;return value;}
int main(){
  std::atomic<bool> ready{false};std::vector<std::thread> workers;
  for(int t=0;t<4;++t)workers.emplace_back([&]{
    while(!ready.load())std::this_thread::yield();
    scratch().a.resize(100);
  });
  ready.store(true);for(auto& worker:workers)worker.join();
}
