#include <algorithm>
#include <numeric>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "generate.h"
#include "rng.h"

namespace railtools {

namespace {

struct WorldParams {
  int64_t lines, isolated_lines;
  int64_t line_len_min, line_len_max;
  int64_t transfer_min, transfer_max;
  int64_t hub_bias;
  int64_t trains_per_day, min_trains_per_line;
  int64_t express_permille, express_keep_transfer_permille, express_keep_other_permille;
  int64_t partial_permille;
  int64_t segment_min, segment_max;
  int64_t dwell_min, dwell_max;
  int64_t first_depart_min, first_depart_max;

  explicit WorldParams(Config& c) {
    lines = c.get("lines", 1, 10000);
    isolated_lines = c.get("isolated_lines", 0, lines - 1);
    line_len_min = c.get("line_len_min", 2, rail::kMaxStopsPerTrain);
    line_len_max = c.get("line_len_max", line_len_min, rail::kMaxStopsPerTrain);
    transfer_min = c.get("transfer_min", 1, line_len_min);
    transfer_max = c.get("transfer_max", transfer_min, line_len_min);
    hub_bias = c.get("hub_bias", 0, 1000000);
    trains_per_day = c.get("trains_per_day", 1, 1000000);
    min_trains_per_line = c.get("min_trains_per_line", 1, 1000);
    if (min_trains_per_line * lines > trains_per_day) {
      throw std::runtime_error("trains_per_day is smaller than lines * min_trains_per_line");
    }
    express_permille = c.get("express_permille", 0, 1000);
    express_keep_transfer_permille = c.get("express_keep_transfer_permille", 0, 1000);
    express_keep_other_permille = c.get("express_keep_other_permille", 0, 1000);
    partial_permille = c.get("partial_permille", 0, 1000);
    segment_min = c.get("segment_min", 1, 1000);
    segment_max = c.get("segment_max", segment_min, 1000);
    dwell_min = c.get("dwell_min", 1, 100);
    dwell_max = c.get("dwell_max", dwell_min, 100);
    first_depart_min = c.get("first_depart_min", 0, rail::kMinutesPerDay - 1);
    first_depart_max = c.get("first_depart_max", first_depart_min, rail::kMinutesPerDay - 1);
    c.check_all_used();
  }
};

const char* const kSyllables[] = {
    "an",   "bai",  "bao",  "bei",  "bin",  "cang", "chang", "chen", "cheng", "chun", "da",   "dong", "feng",
    "fu",   "gang", "gu",   "guan", "hai",  "han",  "he",    "heng", "hong",  "hu",   "hua",  "huai", "jia",
    "jiang", "jin", "jing", "kang", "lan",  "li",   "lin",   "ling", "long",  "lu",   "mei",  "ming", "nan",
    "ning", "ping", "qi",   "qing", "quan", "rui",  "shan",  "shi",  "shui",  "song", "tai",  "tian", "wan",
    "wei",  "xi",   "xin",  "yang", "yi",   "yong", "yu",    "yuan", "yun",   "ze",   "zhou", "zhu",
};
constexpr size_t kSyllableCount = sizeof(kSyllables) / sizeof(kSyllables[0]);

std::string make_station_name(Rng& rng, std::set<std::string>& used) {
  for (int attempt = 0;; ++attempt) {
    std::string name = kSyllables[rng.below(kSyllableCount)];
    name += kSyllables[rng.below(kSyllableCount)];
    name[0] = static_cast<char>(name[0] - 'a' + 'A');
    if (attempt >= 16) name += std::to_string(attempt);
    if (used.insert(name).second) return name;
  }
}

}  // namespace

World generate_world(Config& cfg, uint64_t seed) {
  const WorldParams p(cfg);
  Rng root(seed);
  Rng line_rng = root.fork();
  Rng train_rng = root.fork();
  Rng name_rng = root.fork();
  Rng perm_rng = root.fork();

  // ---- lines: station sequences sharing transfer stations
  std::vector<bool> isolated(p.lines, false);
  {
    std::vector<int64_t> candidates(p.lines - 1);
    std::iota(candidates.begin(), candidates.end(), 1);  // line 0 is never isolated
    line_rng.shuffle(candidates);
    for (int64_t i = 0; i < p.isolated_lines; ++i) isolated[candidates[i]] = true;
  }

  constexpr uint32_t kUnset = UINT32_MAX;
  std::vector<std::vector<uint32_t>> line_stations(p.lines);
  std::vector<std::vector<int64_t>> segment_minutes(p.lines);
  std::vector<uint32_t> connected_pool;  // stations on non-isolated lines
  std::vector<int64_t> lines_through;    // lines serving each station so far
  uint32_t station_count = 0;

  for (int64_t li = 0; li < p.lines; ++li) {
    const int64_t len = line_rng.range(p.line_len_min, p.line_len_max);
    std::vector<uint32_t> seq(len, kUnset);
    if (!isolated[li] && !connected_pool.empty()) {
      const int64_t want = line_rng.range(p.transfer_min, p.transfer_max);
      const int64_t m = std::min<int64_t>(want, static_cast<int64_t>(connected_pool.size()));
      std::vector<int64_t> positions(len);
      std::iota(positions.begin(), positions.end(), 0);
      line_rng.shuffle(positions);
      // Preferential attachment: stations already served by several lines
      // attract new ones, so hubs emerge and the line graph stays shallow.
      std::vector<uint64_t> weights(connected_pool.size());
      for (size_t i = 0; i < connected_pool.size(); ++i) {
        const uint64_t extra = static_cast<uint64_t>(lines_through[connected_pool[i]] - 1);
        weights[i] = 1 + static_cast<uint64_t>(p.hub_bias) * extra;
      }
      const WeightedPicker picker(weights);
      std::vector<uint32_t> chosen;
      while (static_cast<int64_t>(chosen.size()) < m) {
        const uint32_t s = connected_pool[picker.pick(line_rng)];
        if (std::find(chosen.begin(), chosen.end(), s) == chosen.end()) chosen.push_back(s);
      }
      for (uint32_t s : chosen) ++lines_through[s];
      for (int64_t i = 0; i < m; ++i) seq[positions[i]] = chosen[i];
    }
    for (auto& s : seq) {
      if (s != kUnset) continue;
      s = station_count++;
      lines_through.push_back(1);
      if (!isolated[li]) connected_pool.push_back(s);
    }
    line_stations[li] = std::move(seq);
    segment_minutes[li].resize(len - 1);
    for (auto& m : segment_minutes[li]) m = line_rng.range(p.segment_min, p.segment_max);
  }

  // ---- trains: distribute across lines, longer lines get more trains
  std::vector<int64_t> trains_on_line(p.lines, p.min_trains_per_line);
  {
    std::vector<uint64_t> weights(p.lines);
    for (int64_t li = 0; li < p.lines; ++li) weights[li] = line_stations[li].size();
    const WeightedPicker picker(weights);
    for (int64_t extra = p.trains_per_day - p.min_trains_per_line * p.lines; extra > 0; --extra) {
      ++trains_on_line[picker.pick(train_rng)];
    }
  }

  World world;
  int64_t express_no = 0, local_no = 0;
  for (int64_t li = 0; li < p.lines; ++li) {
    const auto& seq = line_stations[li];
    const auto& seg = segment_minutes[li];
    const int64_t n = static_cast<int64_t>(seq.size());
    for (int64_t ti = 0; ti < trains_on_line[li]; ++ti) {
      const bool reverse = train_rng.chance(500);
      int64_t a = 0, b = n - 1;
      if (train_rng.chance(p.partial_permille)) {
        const int64_t span = train_rng.range(std::max<int64_t>(2, (n + 2) / 3), n);
        a = train_rng.range(0, n - span);
        b = a + span - 1;
      }
      const bool express = train_rng.chance(p.express_permille);
      std::vector<int64_t> idx;
      for (int64_t i = a; i <= b; ++i) {
        bool keep = i == a || i == b || !express;
        if (!keep) {
          keep = train_rng.chance(lines_through[seq[i]] > 1 ? p.express_keep_transfer_permille
                                                            : p.express_keep_other_permille);
        }
        if (keep) idx.push_back(i);
      }
      if (reverse) std::reverse(idx.begin(), idx.end());

      WorldTrain train;
      train.name = express ? "G" + std::to_string(++express_no) : "D" + std::to_string(++local_no);
      train.line = static_cast<uint32_t>(li);
      int32_t t = static_cast<int32_t>(train_rng.range(p.first_depart_min, p.first_depart_max));
      train.stops.push_back({seq[idx[0]], t, t});
      for (size_t j = 1; j < idx.size(); ++j) {
        const int64_t lo = std::min(idx[j - 1], idx[j]), hi = std::max(idx[j - 1], idx[j]);
        int64_t travel = 0;
        for (int64_t k = lo; k < hi; ++k) travel += seg[k];
        const int32_t arrive = static_cast<int32_t>(train.stops.back().depart + travel);
        const bool last = j + 1 == idx.size();
        const int32_t depart = last ? arrive : static_cast<int32_t>(arrive + train_rng.range(p.dwell_min, p.dwell_max));
        train.stops.push_back({seq[idx[j]], arrive, depart});
      }
      world.trains.push_back(std::move(train));
    }
  }

  // ---- randomise ids so they carry no structure
  std::vector<uint32_t> station_perm(station_count);
  std::iota(station_perm.begin(), station_perm.end(), 0);
  perm_rng.shuffle(station_perm);  // old id -> new id
  for (auto& train : world.trains)
    for (auto& stop : train.stops) stop.station = station_perm[stop.station];

  std::vector<uint32_t> line_perm(p.lines);
  std::iota(line_perm.begin(), line_perm.end(), 0);
  perm_rng.shuffle(line_perm);
  world.lines.resize(p.lines);
  for (int64_t li = 0; li < p.lines; ++li) {
    auto& line = world.lines[line_perm[li]];
    for (uint32_t s : line_stations[li]) line.stations.push_back(station_perm[s]);
  }
  for (size_t li = 0; li < world.lines.size(); ++li) world.lines[li].name = "L" + std::to_string(li + 1);
  for (auto& train : world.trains) train.line = line_perm[train.line];
  perm_rng.shuffle(world.trains);

  std::set<std::string> used_names;
  world.station_names.resize(station_count);
  for (auto& name : world.station_names) name = make_station_name(name_rng, used_names);

  validate_world(world);
  return world;
}

}  // namespace railtools
