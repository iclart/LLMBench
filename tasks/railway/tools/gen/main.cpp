// railgen - deterministic world and workload generator for the railway task.
//
//   railgen world <world.cfg> <seed> <out.world> [--set key=value]...
//   railgen load  <in.world> <load.cfg> <seed> <out.load> [--set key=value]...
//   railgen stats <in.world> [<in.load>]
#include <algorithm>
#include <cstdio>
#include <exception>
#include <iostream>
#include <string>
#include <vector>

#include "generate.h"

namespace {

using namespace railtools;

uint64_t parse_seed(const std::string& s) {
  size_t pos = 0;
  unsigned long long v = 0;
  try {
    v = std::stoull(s, &pos);
  } catch (const std::exception&) {
    pos = 0;
  }
  if (pos == 0 || pos != s.size() || s[0] == '-') throw std::runtime_error("seed must be a non-negative integer: " + s);
  return v;
}

void print_world_stats(const World& w) {
  std::vector<int> lines_through(w.station_names.size(), 0);
  for (const auto& line : w.lines)
    for (uint32_t s : line.stations) ++lines_through[s];
  const auto transfer_stations = std::count_if(lines_through.begin(), lines_through.end(), [](int n) { return n > 1; });

  size_t isolated = 0;
  for (const auto& line : w.lines) {
    if (std::all_of(line.stations.begin(), line.stations.end(), [&](uint32_t s) { return lines_through[s] == 1; })) {
      ++isolated;
    }
  }

  size_t total_stops = 0, max_stops = 0, express = 0;
  int32_t max_time = 0;
  for (const auto& t : w.trains) {
    total_stops += t.stops.size();
    max_stops = std::max(max_stops, t.stops.size());
    max_time = std::max(max_time, t.stops.back().arrive);
    express += t.name[0] == 'G';
  }
  std::printf("stations           %zu\n", w.station_names.size());
  std::printf("transfer stations  %td\n", transfer_stations);
  std::printf("lines              %zu (isolated %zu)\n", w.lines.size(), isolated);
  std::printf("trains per day     %zu (express %zu)\n", w.trains.size(), express);
  std::printf("stops per train    avg %.1f, max %zu\n", double(total_stops) / double(w.trains.size()), max_stops);
  std::printf("latest arrival     %d min after first-day midnight\n", max_time);
  std::printf("seats on sale      %zu\n", w.trains.size() * size_t(rail::kDays) * rail::kSeatsPerTrainDay);
}

void print_load_stats(const Workload& load) {
  size_t counts[7] = {};
  size_t passengers = 0;
  for (const auto& in : load.intents) {
    ++counts[static_cast<int>(in.kind)];
    if (in.kind == IntentKind::Purchase) passengers += in.passengers;
  }
  const char* names[7] = {"purchase", "browse", "refund", "dup_refund", "bad_query", "bad_book", "bad_refund"};
  std::printf("scenario           %s\n", load.scenario.c_str());
  std::printf("intents            %zu\n", load.intents.size());
  for (int i = 0; i < 7; ++i) std::printf("  %-16s %zu\n", names[i], counts[i]);
  std::printf("purchase pax       %zu\n", passengers);
  std::printf("quiet purchases    %zu\n", load.quiet.size());
}

int usage() {
  std::fprintf(stderr,
               "usage:\n"
               "  railgen world <world.cfg> <seed> <out.world> [--set key=value]...\n"
               "  railgen load  <in.world> <load.cfg> <seed> <out.load> [--set key=value]...\n"
               "  railgen stats <in.world> [<in.load>]\n");
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  std::ios::sync_with_stdio(false);
  std::vector<std::string> args, sets;
  for (int i = 1; i < argc; ++i) {
    if (std::string(argv[i]) == "--set" && i + 1 < argc) {
      sets.push_back(argv[++i]);
    } else {
      args.push_back(argv[i]);
    }
  }
  if (args.empty()) return usage();
  auto load_config = [&](const std::string& path) {
    Config cfg = Config::load(path);
    for (const auto& s : sets) cfg.set(s);
    return cfg;
  };
  try {
    if (args[0] == "world" && args.size() == 4) {
      Config cfg = load_config(args[1]);
      save_world(args[3], generate_world(cfg, parse_seed(args[2])));
    } else if (args[0] == "load" && args.size() == 5) {
      const World world = load_world(args[1]);
      validate_world(world);
      Config cfg = load_config(args[2]);
      save_workload(args[4], generate_workload(world, cfg, parse_seed(args[3])));
    } else if (args[0] == "stats" && (args.size() == 2 || args.size() == 3)) {
      const World world = load_world(args[1]);
      validate_world(world);
      print_world_stats(world);
      if (args.size() == 3) print_load_stats(load_workload(args[2]));
    } else {
      return usage();
    }
  } catch (const std::exception& e) {
    std::fprintf(stderr, "railgen: %s\n", e.what());
    return 1;
  }
  return 0;
}
