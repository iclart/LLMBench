// ABI 2 trusted harness. Engine calls alone are latency-timed; logging is
// buffered per command and serialized only after all measured calls finish.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <unordered_map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "ticket_system_v2.h"
#include "world.h"
#include "benchclock.h"

namespace {
using namespace rail;
using Clock = railtools::BenchClock;
struct Command {
  char op = 0;
  v2::QueryRequest query{};
  BookRequest book{};
  uint64_t target = 0, order = 0;
  v2::CellMask cells = 0;
};
struct Event {
  uint64_t start = 0, end = 0;
  uint32_t caller = 0;
  Status status = Status::Invalid;
  BookResult book{};
  QueryResult query{};
};
uint64_t ns(Clock::duration d) { return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(d).count()); }
BookRequest read_book(std::istream& input) {
  BookRequest b{}; unsigned cls, windows, together;
  if (!(input >> b.leg_count) || b.leg_count > kMaxLegs) throw std::runtime_error("bad listed leg count");
  for (uint32_t i = 0; i < b.leg_count; ++i) input >> b.legs[i].train >> b.legs[i].day >> b.legs[i].from >> b.legs[i].to;
  input >> cls >> b.passenger_count >> windows >> together;
  if (!input || cls > 255 || windows > 31 || together > 1) throw std::runtime_error("malformed book");
  b.seat_class = SeatClass(cls); b.together = together != 0;
  for (uint32_t p = 0; p < kMaxPassengers; ++p) b.window[p] = (windows >> p & 1) != 0;
  return b;
}
Command read_command(std::istream& input) {
  Command c; input >> c.op;
  if (c.op == 'Q') {
    unsigned cls; input >> c.query.from >> c.query.to >> c.query.earliest >> c.query.k >> cls >> c.query.passenger_count;
    if (cls > 255) throw std::runtime_error("invalid class encoding");
    c.query.seat_class = SeatClass(cls);
  } else if (c.op == 'B') c.book = read_book(input);
  else if (c.op == 'R' || c.op == 'E') {
    unsigned mask; input >> c.target >> mask;
    if (mask > 65535) throw std::runtime_error("invalid cell mask encoding");
    c.cells = v2::CellMask(mask);
    if (c.op == 'E') c.book = read_book(input);
  } else throw std::runtime_error("unknown operation");
  if (!input) throw std::runtime_error("truncated command");
  return c;
}
void book_json(std::ostream& out, const BookRequest& b) {
  out << "{\"legs\":[";
  for (uint32_t l = 0; l < b.leg_count; ++l) {
    if (l) out << ',';
    const auto& r = b.legs[l]; out << '[' << r.train << ',' << r.day << ',' << r.from << ',' << r.to << ']';
  }
  unsigned mask = 0;
  for (uint32_t p = 0; p < kMaxPassengers; ++p) mask |= unsigned(b.window[p]) << p;
  out << "],\"class\":" << unsigned(b.seat_class) << ",\"pax\":" << b.passenger_count
      << ",\"window\":" << mask << ",\"together\":" << unsigned(b.together) << '}';
}
void write_event(std::ostream& out, size_t i, const char* phase, const Command& c, const Event& e) {
  out << "{\"kind\":\"event\",\"index\":" << i << ",\"phase\":\"" << phase << "\",\"op\":\"" << c.op
      << "\",\"thread\":" << e.caller
      << ",\"start\":" << e.start << ",\"end\":" << e.end << ",\"status\":" << unsigned(e.status) << ",\"request\":";
  if (c.op == 'Q') {
    const auto& q = c.query;
    out << '[' << q.from << ',' << q.to << ',' << q.earliest << ',' << q.k << ',' << unsigned(q.seat_class) << ',' << q.passenger_count << ']';
  } else if (c.op == 'B') book_json(out, c.book);
  else {
    out << "{\"order\":" << c.order << ",\"cells\":" << c.cells;
    if (c.op == 'E') { out << ",\"replacement\":"; book_json(out, c.book); }
    out << '}';
  }
  if (c.op == 'Q') {
    out << ",\"count\":" << e.query.count << ",\"result\":[";
    for (uint32_t n = 0; n < std::min<uint32_t>(e.query.count, kMaxQueryResults); ++n) {
      if (n) out << ',';
      const auto& it = e.query.items[n]; out << "{\"count\":" << it.leg_count << ",\"legs\":[";
      for (uint32_t l = 0; l < std::min<uint32_t>(it.leg_count, kMaxLegs); ++l) {
        if (l) out << ',';
        const auto& x = it.legs[l]; out << '[' << x.train << ',' << x.day << ',' << x.from << ',' << x.to << ',' << x.depart << ',' << x.arrive << ']';
      }
      out << "]}";
    }
    out << ']';
  } else if (c.op != 'R' && e.status == Status::Ok) {
    out << ",\"order\":" << e.book.order_id << ",\"seats\":[";
    for (uint32_t l = 0; l < c.book.leg_count; ++l) {
      if (l) out << ',';
      out << '[';
      for (uint32_t p = 0; p < std::min<uint32_t>(c.book.passenger_count, kMaxPassengers); ++p) {
        if (p) out << ',';
        const auto& s = e.book.seats[l][p];
        // Encode the letter numerically so even invalid bytes produce valid JSON.
        out << '[' << unsigned(s.car) << ',' << unsigned(s.row) << ',' << unsigned(uint8_t(s.letter)) << ']';
      }
      out << ']';
    }
    out << ']';
  }
  out << "}\n";
}
int run(int argc, char** argv) {
  if (argc != 5) throw std::runtime_error("usage: railv2_run WORLD LOAD THREADS OUT.jsonl");
  unsigned threads = unsigned(std::stoul(argv[3]));
  if (!threads || threads > 256) throw std::runtime_error("threads must be 1..256");
  const auto world = railtools::load_world(argv[1]); railtools::validate_world(world);
  railtools::WorldView view(world);
  std::ifstream input(argv[2]); std::string tag; unsigned version, mode;
  v2::Options options;
  if (!(input >> tag >> version) || tag != "RAILV2" || version != 2) throw std::runtime_error("expected RAILV2 2");
  input >> tag >> mode >> options.service_weight_minutes;
  if (tag != "OPTIONS" || mode > 1) throw std::runtime_error("invalid options");
  options.mode = v2::Mode(mode);
  std::vector<Command> commands; size_t boundary[4]{};
  const char* phases[] = {"PREFILL", "MAIN", "QUIET"};
  for (unsigned p = 0; p < 3; ++p) {
    size_t count; input >> tag >> count;
    if (!input || tag != phases[p] || count > 10000000) throw std::runtime_error("invalid phase header");
    for (size_t i = 0; i < count; ++i) commands.push_back(read_command(input));
    boundary[p + 1] = commands.size();
  }
  input >> tag; if (tag != "END" || (input >> tag)) throw std::runtime_error("invalid load trailer");
  std::vector<Event> events(commands.size());
  auto handles = std::make_unique<std::atomic<uint64_t>[]>(commands.size());
  // Live orders, sharded by order ID so that concurrent callers rarely share
  // a lock; refunds and exchanges still pick orders booked by any thread.
  struct Live { uint64_t id; v2::CellMask active; };
  struct alignas(64) LiveShard { std::mutex mu; std::vector<Live> live; std::unordered_map<uint64_t, size_t> index; };
  constexpr size_t kLiveShards = 64;
  auto shards = std::make_unique<LiveShard[]>(kLiveShards);
  auto shard_of = [&](uint64_t id) -> LiveShard& { return shards[(id * 0x9e3779b97f4a7c15ull) >> 58]; };
  std::mutex init_mu;std::condition_variable init_cv;bool init_done=false;
  std::thread watchdog([&]{
    std::unique_lock<std::mutex> lock(init_mu);
    if(!init_cv.wait_for(lock,std::chrono::seconds(30),[&]{return init_done;}))std::_Exit(124);
    lock.unlock();
#ifdef _WIN32
    Sleep(INFINITE);
#endif
  });
  const auto epoch = Clock::now();
  static v2::TicketSystem* engine = v2::create_ticket_system();
  engine->init(view.view(), options); const auto initialized = Clock::now();
  {std::lock_guard<std::mutex> lock(init_mu);init_done=true;}init_cv.notify_one();
#ifdef _WIN32
  watchdog.detach();
#else
  watchdog.join();
#endif
  auto play = [&](size_t i, uint32_t caller=0) {
    auto& c = commands[i]; auto& event = events[i];
    event.caller=caller;
    if (c.op == 'R' || c.op == 'E') {
      c.order = c.target < commands.size() ? handles[c.target].load(std::memory_order_acquire) : 0;
      if (c.target == UINT64_MAX) {
        // A pseudo-random live order: a shard from the command index, then a
        // random entry of the first nonempty shard from there.
        uint64_t state = (i + 1) * 0x9e3779b97f4a7c15ull;
        state ^= state >> 12; state ^= state << 25; state ^= state >> 27;
        const size_t first = size_t(state >> 58);
        for (size_t k = 0; k < kLiveShards; ++k) {
          auto& shard = shards[(first + k) % kLiveShards];
          std::lock_guard<std::mutex> lock(shard.mu);
          if (shard.live.empty()) continue;
          const auto& chosen = shard.live[(state * 2685821657736338717ull) % shard.live.size()];
          c.order = chosen.id;
          c.cells &= chosen.active;
          if (!c.cells) c.cells = v2::CellMask(chosen.active & -chosen.active);
          if (c.op == 'E') {
            c.book.passenger_count = 0;
            for (unsigned p = 0; p < 5; ++p) c.book.passenger_count += (c.cells & (7 << (p * 3))) != 0;
          }
          break;
        }
      }
    }
    event.start = ns(Clock::now() - epoch);
    if (c.op == 'Q') event.status = engine->query(c.query, &event.query);
    else if (c.op == 'B') event.status = engine->book(c.book, &event.book);
    else if (c.op == 'R') event.status = engine->refund({c.order, c.cells});
    else event.status = engine->exchange({c.order, c.cells, c.book}, &event.book);
    event.end = ns(Clock::now() - epoch);
    if ((c.op == 'B' || c.op == 'E') && event.status == Status::Ok) handles[i].store(event.book.order_id, std::memory_order_release);
    if (event.status == Status::Ok && c.op != 'Q') {
      if (c.op == 'R' || c.op == 'E') {
        auto& shard = shard_of(c.order);
        std::lock_guard<std::mutex> lock(shard.mu);
        auto pos = shard.index.find(c.order);
        if (pos != shard.index.end()) {
          const size_t index = pos->second;
          shard.live[index].active &= ~c.cells;
          if (!shard.live[index].active) {
            if (index + 1 != shard.live.size()) {
              shard.live[index] = shard.live.back();
              shard.index[shard.live[index].id] = index;
            }
            shard.live.pop_back(); shard.index.erase(pos);
          }
        }
      }
      if (c.op == 'B' || c.op == 'E') {
        auto& shard = shard_of(event.book.order_id);
        std::lock_guard<std::mutex> lock(shard.mu);
        shard.index[event.book.order_id] = shard.live.size();
        shard.live.push_back({event.book.order_id, v2::all_cells(c.book.passenger_count, c.book.leg_count)});
      }
    }
  };
  for (size_t i = 0; i < boundary[1]; ++i) play(i);
  std::atomic<bool> ready{false}; std::atomic<unsigned> completed{0}; std::vector<std::thread> pool;
  for (unsigned t = 0; t < threads; ++t) pool.emplace_back([&, t] {
    while (!ready.load(std::memory_order_acquire)) std::this_thread::yield();
    for (size_t i = boundary[1] + t; i < boundary[2]; i += threads) play(i,t+1);
#ifdef _WIN32
    if (completed.fetch_add(1, std::memory_order_release) + 1 == threads) completed.notify_one();
    Sleep(INFINITE);  // See v1 harness: avoid MinGW's broken TLS teardown.
#endif
  });
  const auto start = Clock::now(); ready.store(true, std::memory_order_release);
#ifdef _WIN32
  for (auto& thread : pool) thread.detach();
  for (auto n = completed.load(std::memory_order_acquire); n != threads; n = completed.load(std::memory_order_acquire)) completed.wait(n);
#else
  for (auto& thread : pool) thread.join();
#endif
  const auto finish = Clock::now();
  for (size_t i = boundary[2]; i < commands.size(); ++i) play(i);
  std::ofstream out(argv[4]);
  if (!out) throw std::runtime_error("cannot write log");
  out << "{\"kind\":\"header\",\"schema_version\":2,\"abi_version\":2,\"mode\":" << mode
      << ",\"lambda\":" << options.service_weight_minutes << ",\"threads\":" << threads
      << ",\"init_ns\":" << ns(initialized - epoch) << ",\"main_ns\":" << ns(finish - start) << ",\"calls\":" << commands.size() << "}\n";
  for (unsigned phase = 0; phase < 3; ++phase)
    for (size_t i = boundary[phase]; i < boundary[phase + 1]; ++i) write_event(out, i, phases[phase], commands[i], events[i]);
  if (!out) throw std::runtime_error("writing log failed");
#ifdef _WIN32
  out.close();
  if (!out) throw std::runtime_error("closing log failed");
  std::fflush(nullptr);
  ExitProcess(0);
#endif
  return 0;
}
}  // namespace
int main(int argc, char** argv) {
  try { return run(argc, argv); }
  catch (const std::exception& error) { std::cerr << "railv2: " << error.what() << '\n'; return 2; }
}
