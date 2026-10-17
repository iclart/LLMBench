#include "benchlog.h"

#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>

namespace railtools {

namespace {

constexpr char kMagic[8] = {'R', 'A', 'I', 'L', 'L', 'O', 'G', '1'};

struct Header {
  char magic[8];
  uint32_t threads, probe_count;
  uint64_t seed, init_ns, main_ns, quiet_ns, main_intents, quiet_intents;
  uint64_t event_count, seat_block_count, audit_ns;
};

struct FileCloser {
  void operator()(std::FILE* f) const { std::fclose(f); }
};
using File = std::unique_ptr<std::FILE, FileCloser>;

}  // namespace

void write_bench_log(const std::string& path, const BenchLog& log) {
  File f(std::fopen(path.c_str(), "wb"));
  if (!f) throw std::runtime_error("cannot write " + path);
  Header h{};
  std::memcpy(h.magic, kMagic, sizeof kMagic);
  h.threads = log.threads;
  h.seed = log.seed;
  h.init_ns = log.init_ns;
  h.main_ns = log.main_ns;
  h.quiet_ns = log.quiet_ns;
  h.main_intents = log.main_intents;
  h.quiet_intents = log.quiet_intents;
  h.event_count = log.events.size();
  h.seat_block_count = log.seat_blocks.size();
  h.probe_count = static_cast<uint32_t>(log.probes.size());
  h.audit_ns = log.audit_ns;
  bool ok = std::fwrite(&h, sizeof h, 1, f.get()) == 1;
  if (!log.events.empty()) ok &= std::fwrite(log.events.data(), sizeof(Event), log.events.size(), f.get()) == log.events.size();
  if (!log.seat_blocks.empty()) {
    ok &= std::fwrite(log.seat_blocks.data(), sizeof(SeatBlock), log.seat_blocks.size(), f.get()) == log.seat_blocks.size();
  }
  if (!log.probes.empty()) ok &= std::fwrite(log.probes.data(), sizeof(Probe), log.probes.size(), f.get()) == log.probes.size();
  if (!ok) throw std::runtime_error("error writing " + path);
}

BenchLog read_bench_log(const std::string& path) {
  File f(std::fopen(path.c_str(), "rb"));
  if (!f) throw std::runtime_error("cannot read " + path);
  Header h{};
  if (std::fread(&h, sizeof h, 1, f.get()) != 1 || std::memcmp(h.magic, kMagic, sizeof kMagic) != 0) {
    throw std::runtime_error(path + " is not a railbench log");
  }
  BenchLog log;
  log.threads = h.threads;
  log.seed = h.seed;
  log.init_ns = h.init_ns;
  log.main_ns = h.main_ns;
  log.quiet_ns = h.quiet_ns;
  log.main_intents = h.main_intents;
  log.quiet_intents = h.quiet_intents;
  log.audit_ns = h.audit_ns;
  log.events.resize(h.event_count);
  log.seat_blocks.resize(h.seat_block_count);
  log.probes.resize(h.probe_count);
  bool ok = true;
  if (h.event_count) ok &= std::fread(log.events.data(), sizeof(Event), h.event_count, f.get()) == h.event_count;
  if (h.seat_block_count) {
    ok &= std::fread(log.seat_blocks.data(), sizeof(SeatBlock), h.seat_block_count, f.get()) == h.seat_block_count;
  }
  if (h.probe_count) ok &= std::fread(log.probes.data(), sizeof(Probe), h.probe_count, f.get()) == h.probe_count;
  if (!ok) throw std::runtime_error(path + " is truncated");
  return log;
}

}  // namespace railtools
