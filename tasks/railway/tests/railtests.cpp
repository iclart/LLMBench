// Self-checks for the railway tools. Run via ctest, or directly:
//   railtests [--print-golden]
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "config.h"
#include "generate.h"
#include "rules.h"
#include "ticket_system.h"
#include "workload.h"
#include "world.h"

using namespace railtools;

namespace {

int failures = 0;

#define CHECK(cond)                                                       \
  do {                                                                    \
    if (!(cond)) {                                                        \
      std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
      ++failures;                                                         \
    }                                                                     \
  } while (0)

const std::string kConfigDir = std::string(RAIL_SOURCE_DIR) + "/configs/public/";
const char* const kScenarios[] = {"daily", "spring", "refund_wave"};
constexpr uint64_t kGoldenSeed = 20261017;

// FNV-1a, 64 bit.
uint64_t fnv1a(const std::string& s) {
  uint64_t h = 0xcbf29ce484222325ull;
  for (unsigned char c : s) {
    h ^= c;
    h *= 0x100000001b3ull;
  }
  return h;
}

World make_world(uint64_t seed) {
  Config cfg = Config::load(kConfigDir + "world.cfg");
  return generate_world(cfg, seed);
}

Workload make_load(const World& world, const char* scenario, uint64_t seed) {
  Config cfg = Config::load(kConfigDir + scenario + ".cfg");
  return generate_workload(world, cfg, seed);
}

std::string text_of(const World& w) {
  std::ostringstream out;
  write_world(out, w);
  return out.str();
}

std::string text_of(const Workload& l) {
  std::ostringstream out;
  write_workload(out, l);
  return out.str();
}

void test_config_rejects_unknown_and_missing_keys() {
  Config c = Config::parse("a = 1\nb = 2 # comment\n", "test");
  CHECK(c.get("a") == 1);
  bool threw = false;
  try {
    c.check_all_used();
  } catch (const std::exception&) {
    threw = true;
  }
  CHECK(threw);
  threw = false;
  try {
    c.get("missing");
  } catch (const std::exception&) {
    threw = true;
  }
  CHECK(threw);
}

void test_world_is_deterministic_and_round_trips() {
  const World a = make_world(7), b = make_world(7), c = make_world(8);
  const std::string ta = text_of(a);
  CHECK(ta == text_of(b));
  CHECK(ta != text_of(c));

  std::istringstream in(ta);
  const World back = read_world(in);
  validate_world(back);
  CHECK(text_of(back) == ta);

  CHECK(a.trains.size() == 1000);
  for (const auto& t : a.trains) CHECK(t.stops.size() <= size_t(rail::kMaxStopsPerTrain));
  const auto cross = std::count_if(a.trains.begin(), a.trains.end(), [](const WorldTrain& t) { return t.line2 != kNoLine; });
  CHECK(cross > 50 && cross < 200);  // cross_line_permille = 120

  // Version 1 files (one line per train, no line count) still load.
  std::string v1 = "RAILWORLD 1\nSTATIONS 3\nA\nB\nC\nLINES 1\nL1 3 0 1 2\nTRAINS 1\nD1 0 2 0 600 600 2 700 700\nEND\n";
  std::istringstream in1(v1);
  const World old = read_world(in1);
  validate_world(old);
  CHECK(old.trains[0].line == 0 && old.trains[0].line2 == kNoLine);
}

void test_world_view_matches_world() {
  const World w = make_world(3);
  const WorldView view(w);
  const rail::WorldData& d = view.view();
  CHECK(d.station_count == w.station_names.size());
  CHECK(d.train_count == w.trains.size());
  CHECK(d.line_count == w.lines.size());
  for (uint32_t t = 0; t < d.train_count; ++t) {
    const auto& tr = d.trains[t];
    CHECK(tr.stop_count == w.trains[t].stops.size());
    CHECK(std::strcmp(tr.name, w.trains[t].name.c_str()) == 0);
    for (uint32_t i = 0; i < tr.stop_count; ++i) {
      CHECK(d.stops[tr.stop_begin + i].station == w.trains[t].stops[i].station);
      CHECK(d.stops[tr.stop_begin + i].arrive == w.trains[t].stops[i].arrive);
    }
  }
  for (uint32_t l = 0; l < d.line_count; ++l) {
    CHECK(d.lines[l].station_count == w.lines[l].stations.size());
    CHECK(d.line_stations[d.lines[l].station_begin] == w.lines[l].stations[0]);
  }
}

void test_validator_catches_broken_worlds() {
  World w = make_world(5);
  validate_world(w);
  auto expect_invalid = [](World broken) {
    try {
      validate_world(broken);
    } catch (const std::exception&) {
      return true;
    }
    return false;
  };
  World dup = w;
  dup.trains[0].stops[1].station = dup.trains[0].stops[0].station;
  CHECK(expect_invalid(dup));
  // A cross-line train must switch lines at a shared station: claiming a
  // different second line, or none, breaks it.
  const auto cross = std::find_if(w.trains.begin(), w.trains.end(), [](const WorldTrain& t) { return t.line2 != kNoLine; });
  CHECK(cross != w.trains.end());
  const size_t ci = static_cast<size_t>(cross - w.trains.begin());
  World single = w;
  single.trains[ci].line2 = kNoLine;
  CHECK(expect_invalid(single));
  World wrong = w;
  wrong.trains[ci].line2 = (wrong.trains[ci].line2 + 1) % uint32_t(w.lines.size());
  if (wrong.trains[ci].line2 == wrong.trains[ci].line) wrong.trains[ci].line2 = (wrong.trains[ci].line2 + 1) % uint32_t(w.lines.size());
  CHECK(expect_invalid(wrong));
  World slow = w;
  slow.trains[0].stops[1].arrive = slow.trains[0].stops[0].depart;
  CHECK(expect_invalid(slow));
  World late = w;
  for (auto& s : late.trains[0].stops) {
    s.arrive += 2000;
    s.depart += 2000;
  }
  CHECK(expect_invalid(late));
}

void test_workloads_round_trip_and_are_well_formed() {
  const World w = make_world(11);
  for (const char* scenario : kScenarios) {
    const Workload a = make_load(w, scenario, 99);
    CHECK(a.scenario == scenario);
    const std::string text = text_of(a);
    CHECK(text == text_of(make_load(w, scenario, 99)));
    std::istringstream in(text);
    const Workload back = read_workload(in);
    CHECK(text_of(back) == text);

    size_t bad_queries = 0, bad_books = 0;
    for (const auto* phase : {&a.intents, &a.quiet}) {
      for (const auto& in : *phase) {
        switch (in.kind) {
          case IntentKind::Purchase:
          case IntentKind::Browse:
            CHECK(is_valid_query(w, in.query));
            break;
          case IntentKind::BadQuery:
            ++bad_queries;
            CHECK(!is_valid_query(w, in.query));
            break;
          case IntentKind::BadBook:
            ++bad_books;
            CHECK(!is_valid_book(w, a.bad_books[in.bad_book]));
            break;
          default:
            break;
        }
      }
    }
    CHECK(bad_queries > 0);
    CHECK(bad_books > 0);
    for (const auto& in : a.quiet) CHECK(in.kind == IntentKind::Purchase);
  }
}

void test_rules() {
  const World w = make_world(13);
  const auto& t0 = w.trains[0];
  rail::BookRequest b{};
  b.leg_count = 1;
  b.legs[0] = {0, 0, t0.stops[0].station, t0.stops.back().station};
  b.seat_class = rail::SeatClass::Second;
  b.passenger_count = 1;
  CHECK(is_valid_book(w, b));
  b.legs[0].day = 3;
  CHECK(!is_valid_book(w, b));
  b.legs[0].day = 2;
  CHECK(is_valid_book(w, b));
  std::swap(b.legs[0].from, b.legs[0].to);
  CHECK(!is_valid_book(w, b));

  rail::QueryRequest q{t0.stops[0].station, t0.stops.back().station, 0, 5};
  CHECK(is_valid_query(w, q));
  q.earliest = rail::kDays * rail::kMinutesPerDay;
  CHECK(!is_valid_query(w, q));
}

void test_stub_links_against_interface() {
  const World w = make_world(17);
  const WorldView view(w);
  std::unique_ptr<rail::TicketSystem> sys(rail::create_ticket_system());
  sys->init(view.view());
  rail::QueryResult qr{};
  CHECK(sys->query({0, 1, 0, 5}, &qr) == rail::Status::Ok);
  CHECK(sys->refund(0) == rail::Status::NotFound);
}

// Generator output must be byte-identical on every platform. These hashes
// were produced on Linux; CI checks them on Windows too.
// Regenerate with `railtests --print-golden` only when a format or generator
// change is intended.
const uint64_t kGoldenWorld = 0x421fd668499557ccull;
const uint64_t kGoldenLoads[] = {0xbda5cc579bf9ec5cull, 0x42a520978e1635f4ull, 0x612ee89b04ea37d5ull};

void test_golden_output(bool print) {
  const World w = make_world(kGoldenSeed);
  const uint64_t hw = fnv1a(text_of(w));
  if (print) std::printf("world        0x%016llxull\n", static_cast<unsigned long long>(hw));
  else CHECK(hw == kGoldenWorld);
  for (int i = 0; i < 3; ++i) {
    const uint64_t hl = fnv1a(text_of(make_load(w, kScenarios[i], kGoldenSeed + 1 + i)));
    if (print) std::printf("%-12s 0x%016llxull\n", kScenarios[i], static_cast<unsigned long long>(hl));
    else CHECK(hl == kGoldenLoads[i]);
  }
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::strcmp(argv[1], "--print-golden") == 0) {
    test_golden_output(true);
    return 0;
  }
  try {
    test_config_rejects_unknown_and_missing_keys();
    test_world_is_deterministic_and_round_trips();
    test_world_view_matches_world();
    test_validator_catches_broken_worlds();
    test_workloads_round_trip_and_are_well_formed();
    test_rules();
    test_stub_links_against_interface();
    test_golden_output(false);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "unexpected exception: %s\n", e.what());
    return 1;
  }
  if (failures) {
    std::fprintf(stderr, "%d check(s) failed\n", failures);
    return 1;
  }
  std::printf("all railway tool tests passed\n");
  return 0;
}
