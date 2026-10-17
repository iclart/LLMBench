// In-memory railway world used by the tools, its text file format, and the
// conversion to the contestant-facing rail::WorldData view.
//
// File format (ASCII, one record per line, single spaces):
//
//   RAILWORLD 1
//   STATIONS <n>
//   <name>                                    n lines, station id = line index
//   LINES <m>
//   <name> <k> <s_0> ... <s_k-1>              m lines
//   TRAINS <t>
//   <name> <line> <k> <st arr dep> * k        t lines
//   END
#pragma once

#include <cstdint>
#include <iosfwd>
#include <string>
#include <vector>

#include "ticket_system.h"

namespace railtools {

struct WorldLine {
  std::string name;
  std::vector<uint32_t> stations;
};

struct WorldTrain {
  std::string name;
  uint32_t line = 0;
  std::vector<rail::Stop> stops;
};

struct World {
  std::vector<std::string> station_names;
  std::vector<WorldLine> lines;
  std::vector<WorldTrain> trains;
};

void write_world(std::ostream& out, const World& world);
World read_world(std::istream& in);
World load_world(const std::string& path);
void save_world(const std::string& path, const World& world);

// Throws std::runtime_error describing the first violated invariant.
void validate_world(const World& world);

// Owns the flat arrays behind a rail::WorldData. Must outlive every
// TicketSystem that was initialised from view().
class WorldView {
 public:
  explicit WorldView(const World& world);
  WorldView(const WorldView&) = delete;
  WorldView& operator=(const WorldView&) = delete;

  const rail::WorldData& view() const { return data_; }

 private:
  std::vector<std::string> station_names_;
  std::vector<const char*> station_name_ptrs_;
  std::vector<std::string> line_names_;
  std::vector<rail::Line> lines_;
  std::vector<uint32_t> line_stations_;
  std::vector<std::string> train_names_;
  std::vector<rail::Train> trains_;
  std::vector<rail::Stop> stops_;
  rail::WorldData data_{};
};

}  // namespace railtools
