#include "answers.h"

#include <sstream>

#include "workload.h"

namespace railtools {

bool parse_answer(const std::string& line, Answer* out) {
  out->hash = fnv1a(line.data(), line.size());
  out->items.clear();
  std::istringstream in(line);
  if (!(in >> out->status)) return false;
  if (out->status != static_cast<int>(rail::Status::Ok)) return true;
  uint32_t count = 0;
  if (!(in >> count) || count > uint32_t(rail::kMaxQueryResults)) return false;
  // Itineraries are separated by '|'; legs are 6 integers each.
  std::string tok;
  rail::Itinerary cur{};
  bool open = false;
  auto close = [&] {
    if (open) out->items.push_back(cur);
    cur = rail::Itinerary{};
    open = false;
  };
  while (in >> tok) {
    if (tok == "|") {
      close();
      open = true;
      continue;
    }
    if (!open || cur.leg_count >= uint32_t(rail::kMaxLegs)) return false;
    rail::Leg& l = cur.legs[cur.leg_count++];
    int64_t v[6];
    try {
      v[0] = std::stoll(tok);
    } catch (const std::exception&) {
      return false;
    }
    for (int i = 1; i < 6; ++i)
      if (!(in >> v[i])) return false;
    l = {uint32_t(v[0]), uint32_t(v[1]), uint32_t(v[2]), uint32_t(v[3]), int32_t(v[4]), int32_t(v[5])};
  }
  close();
  return out->items.size() == count;
}

std::vector<uint32_t> query_ordinals(const Workload& load, bool quiet) {
  std::vector<uint32_t> idx;
  const auto& intents = quiet ? load.quiet : load.intents;
  for (uint32_t i = 0; i < intents.size(); ++i) {
    const IntentKind k = intents[i].kind;
    if (k == IntentKind::Purchase || k == IntentKind::Browse || k == IntentKind::BadQuery) idx.push_back(i);
  }
  return idx;
}

}  // namespace railtools
