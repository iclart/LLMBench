#include "world.h"

#include <fstream>
#include <istream>
#include <ostream>
#include <stdexcept>

#include "text_io.h"

namespace railtools {

void write_world(std::ostream& out, const World& world) {
  out << "RAILWORLD 2\n";
  out << "STATIONS " << world.station_names.size() << '\n';
  for (const auto& name : world.station_names) out << name << '\n';
  out << "LINES " << world.lines.size() << '\n';
  for (const auto& line : world.lines) {
    out << line.name << ' ' << line.stations.size();
    for (uint32_t s : line.stations) out << ' ' << s;
    out << '\n';
  }
  out << "TRAINS " << world.trains.size() << '\n';
  for (const auto& train : world.trains) {
    out << train.name << ' ' << (train.line2 == kNoLine ? 1 : 2) << ' ' << train.line;
    if (train.line2 != kNoLine) out << ' ' << train.line2;
    out << ' ' << train.stops.size();
    for (const auto& stop : train.stops) out << ' ' << stop.station << ' ' << stop.arrive << ' ' << stop.depart;
    out << '\n';
  }
  out << "END\n";
}

World read_world(std::istream& in) {
  TokenReader r(in, "world");
  World world;
  r.expect("RAILWORLD");
  const uint32_t version = r.u32();
  if (version != 1 && version != 2) r.fail("unsupported world format version");

  r.expect("STATIONS");
  world.station_names.resize(r.u32());
  for (auto& name : world.station_names) name = r.word();

  r.expect("LINES");
  world.lines.resize(r.u32());
  for (auto& line : world.lines) {
    line.name = r.word();
    line.stations.resize(r.u32());
    for (auto& s : line.stations) s = r.u32();
  }

  r.expect("TRAINS");
  world.trains.resize(r.u32());
  for (auto& train : world.trains) {
    train.name = r.word();
    const uint32_t lines = version == 1 ? 1 : r.u32();
    if (lines != 1 && lines != 2) r.fail("train " + train.name + " must run on 1 or 2 lines");
    train.line = r.u32();
    if (lines == 2) train.line2 = r.u32();
    const uint32_t k = r.u32();
    if (k > rail::kMaxStopsPerTrain) r.fail("train " + train.name + " has too many stops");
    train.stops.resize(k);
    for (auto& stop : train.stops) {
      stop.station = r.u32();
      stop.arrive = r.i32();
      stop.depart = r.i32();
    }
  }
  r.expect("END");
  return world;
}

World load_world(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot open world file " + path);
  return read_world(in);
}

void save_world(const std::string& path, const World& world) {
  std::ofstream out(path, std::ios::binary);
  if (!out) throw std::runtime_error("cannot write world file " + path);
  write_world(out, world);
  if (!out) throw std::runtime_error("error writing world file " + path);
}

void validate_world(const World& world) {
  const auto fail = [](const std::string& msg) { throw std::runtime_error("invalid world: " + msg); };
  const size_t n = world.station_names.size();
  if (n < 2) fail("fewer than 2 stations");
  if (world.lines.empty()) fail("no lines");
  if (world.trains.empty()) fail("no trains");

  for (const auto& name : world.station_names) {
    if (name.empty()) fail("empty station name");
  }

  // position[line][station] = index of the station on the line, or -1.
  std::vector<std::vector<int>> position(world.lines.size(), std::vector<int>(n, -1));
  std::vector<bool> on_some_line(n, false);
  for (size_t li = 0; li < world.lines.size(); ++li) {
    const auto& line = world.lines[li];
    if (line.stations.size() < 2) fail("line " + line.name + " has fewer than 2 stations");
    for (size_t i = 0; i < line.stations.size(); ++i) {
      const uint32_t s = line.stations[i];
      if (s >= n) fail("line " + line.name + " references unknown station");
      if (position[li][s] != -1) fail("line " + line.name + " visits a station twice");
      position[li][s] = static_cast<int>(i);
      on_some_line[s] = true;
    }
  }
  for (size_t s = 0; s < n; ++s) {
    if (!on_some_line[s]) fail("station " + world.station_names[s] + " is on no line");
  }

  // Stops [from, to] of a train lie on line li in strictly monotone order.
  auto runs_along = [&](const WorldTrain& train, uint32_t li, size_t from, size_t to) {
    const auto& pos = position[li];
    int direction = 0;
    for (size_t i = from; i <= to; ++i) {
      if (pos[train.stops[i].station] < 0) return false;
      if (i == from) continue;
      const int step = pos[train.stops[i].station] - pos[train.stops[i - 1].station];
      const int dir = step > 0 ? 1 : -1;
      if (step == 0 || (direction != 0 && dir != direction)) return false;
      direction = dir;
    }
    return true;
  };

  for (const auto& train : world.trains) {
    const std::string who = "train " + train.name;
    if (train.line >= world.lines.size()) fail(who + " references unknown line");
    const bool cross = train.line2 != kNoLine;
    if (cross && (train.line2 >= world.lines.size() || train.line2 == train.line)) fail(who + " has a bad second line");
    const auto& stops = train.stops;
    if (stops.size() < 2 || stops.size() > rail::kMaxStopsPerTrain) fail(who + " has a bad stop count");
    std::vector<bool> visited(n, false);
    for (size_t i = 0; i < stops.size(); ++i) {
      const auto& stop = stops[i];
      if (stop.station >= n) fail(who + " references unknown station");
      if (visited[stop.station]) fail(who + " visits a station twice");
      visited[stop.station] = true;
      const bool terminal = i == 0 || i + 1 == stops.size();
      if (terminal ? stop.arrive != stop.depart : stop.arrive >= stop.depart) {
        fail(who + " has bad arrive/depart at stop " + std::to_string(i));
      }
      if (i == 0) {
        if (stop.depart < 0 || stop.depart >= rail::kMinutesPerDay) fail(who + " departs outside its first day");
        continue;
      }
      if (stop.arrive <= stops[i - 1].depart) fail(who + " has non-increasing times at stop " + std::to_string(i));
    }
    const size_t last = stops.size() - 1;
    if (!cross) {
      if (!runs_along(train, train.line, 0, last)) fail(who + " leaves its line or changes direction along it");
      continue;
    }
    // Some junction stop j: the first line up to j, the second from j on.
    bool ok = false;
    for (size_t j = 1; j < last && !ok; ++j) {
      ok = runs_along(train, train.line, 0, j) && runs_along(train, train.line2, j, last);
    }
    if (!ok) fail(who + " does not run along its first line to a shared station and then along its second");
  }
}

WorldView::WorldView(const World& world)
    : station_names_(world.station_names) {
  station_name_ptrs_.reserve(station_names_.size());
  for (const auto& name : station_names_) station_name_ptrs_.push_back(name.c_str());

  line_names_.reserve(world.lines.size());
  for (const auto& line : world.lines) line_names_.push_back(line.name);
  for (size_t i = 0; i < world.lines.size(); ++i) {
    const auto& line = world.lines[i];
    lines_.push_back({static_cast<uint32_t>(line_stations_.size()), static_cast<uint32_t>(line.stations.size()),
                      line_names_[i].c_str()});
    line_stations_.insert(line_stations_.end(), line.stations.begin(), line.stations.end());
  }

  train_names_.reserve(world.trains.size());
  for (const auto& train : world.trains) train_names_.push_back(train.name);
  for (size_t i = 0; i < world.trains.size(); ++i) {
    const auto& train = world.trains[i];
    trains_.push_back({train.line, static_cast<uint32_t>(stops_.size()), static_cast<uint32_t>(train.stops.size()),
                       train_names_[i].c_str()});
    stops_.insert(stops_.end(), train.stops.begin(), train.stops.end());
  }

  data_.station_count = static_cast<uint32_t>(station_names_.size());
  data_.station_names = station_name_ptrs_.data();
  data_.line_count = static_cast<uint32_t>(lines_.size());
  data_.lines = lines_.data();
  data_.line_stations = line_stations_.data();
  data_.train_count = static_cast<uint32_t>(trains_.size());
  data_.trains = trains_.data();
  data_.stops = stops_.data();
}

}  // namespace railtools
