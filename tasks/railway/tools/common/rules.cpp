#include "rules.h"

namespace railtools {

bool resolve_leg(const World& world, const rail::LegRef& leg, ResolvedLeg* out) {
  if (leg.train >= world.trains.size() || leg.day >= static_cast<uint32_t>(rail::kDays)) return false;
  if (leg.from >= world.station_names.size() || leg.to >= world.station_names.size()) return false;
  const auto& stops = world.trains[leg.train].stops;
  int from = -1, to = -1;
  for (size_t i = 0; i < stops.size(); ++i) {
    if (stops[i].station == leg.from) from = static_cast<int>(i);
    if (stops[i].station == leg.to) to = static_cast<int>(i);
  }
  if (from < 0 || to < 0 || from >= to) return false;
  const int32_t base = static_cast<int32_t>(leg.day) * rail::kMinutesPerDay;
  out->from_index = static_cast<uint32_t>(from);
  out->to_index = static_cast<uint32_t>(to);
  out->depart = base + stops[from].depart;
  out->arrive = base + stops[to].arrive;
  return true;
}

bool is_valid_itinerary(const World& world, const rail::LegRef* legs, uint32_t n) {
  if (n < 1 || n > static_cast<uint32_t>(rail::kMaxLegs)) return false;
  ResolvedLeg resolved[rail::kMaxLegs];
  for (uint32_t i = 0; i < n; ++i) {
    if (!resolve_leg(world, legs[i], &resolved[i])) return false;
  }
  for (uint32_t i = 0; i + 1 < n; ++i) {
    if (legs[i].to != legs[i + 1].from) return false;
    if (resolved[i + 1].depart < resolved[i].arrive + rail::kMinTransferMinutes) return false;
  }
  // Origin, transfer stations and destination are pairwise distinct.
  uint32_t stations[rail::kMaxLegs + 1];
  stations[0] = legs[0].from;
  for (uint32_t i = 0; i < n; ++i) stations[i + 1] = legs[i].to;
  for (uint32_t i = 0; i <= n; ++i)
    for (uint32_t j = i + 1; j <= n; ++j)
      if (stations[i] == stations[j]) return false;
  // No train-day twice.
  for (uint32_t i = 0; i < n; ++i)
    for (uint32_t j = i + 1; j < n; ++j)
      if (legs[i].train == legs[j].train && legs[i].day == legs[j].day) return false;
  return true;
}

bool is_valid_query(const World& world, const rail::QueryRequest& req) {
  const auto stations = world.station_names.size();
  return req.from < stations && req.to < stations && req.from != req.to && req.earliest >= 0 &&
         req.earliest < rail::kDays * rail::kMinutesPerDay && req.k >= 1 &&
         req.k <= static_cast<uint32_t>(rail::kMaxQueryResults);
}

bool is_valid_book(const World& world, const rail::BookRequest& req) {
  if (req.leg_count < 1 || req.leg_count > static_cast<uint32_t>(rail::kMaxLegs)) return false;
  if (static_cast<uint8_t>(req.seat_class) >= rail::kSeatClassCount) return false;
  if (req.passenger_count < 1 || req.passenger_count > static_cast<uint32_t>(rail::kMaxPassengers)) return false;
  return is_valid_itinerary(world, req.legs, req.leg_count);
}

}  // namespace railtools
