#include "workload.h"

#include <algorithm>
#include <fstream>
#include <istream>
#include <ostream>
#include <stdexcept>

#include "text_io.h"

namespace railtools {

namespace {

void write_query(std::ostream& out, const rail::QueryRequest& q) {
  out << ' ' << q.from << ' ' << q.to << ' ' << q.earliest << ' ' << q.k;
}

void write_intent(std::ostream& out, const Workload& load, const Intent& in) {
  switch (in.kind) {
    case IntentKind::Purchase:
      out << 'P';
      write_query(out, in.query);
      out << ' ' << int(in.seat_class) << ' ' << int(in.passengers) << ' ' << int(in.window_mask) << ' '
          << int(in.together);
      break;
    case IntentKind::Browse:
      out << 'Q';
      write_query(out, in.query);
      break;
    case IntentKind::Refund:
      out << 'R';
      break;
    case IntentKind::DupRefund:
      out << "RD";
      break;
    case IntentKind::BadQuery:
      out << "XQ";
      write_query(out, in.query);
      break;
    case IntentKind::BadBook: {
      const auto& b = load.bad_books.at(in.bad_book);
      const uint32_t listed = std::min<uint32_t>(b.leg_count, rail::kMaxLegs);
      out << "XB " << b.leg_count << ' ' << listed;
      for (uint32_t i = 0; i < listed; ++i) {
        const auto& leg = b.legs[i];
        out << ' ' << leg.train << ' ' << leg.day << ' ' << leg.from << ' ' << leg.to;
      }
      uint32_t mask = 0;
      for (int p = 0; p < rail::kMaxPassengers; ++p) mask |= uint32_t(b.window[p]) << p;
      out << ' ' << int(b.seat_class) << ' ' << b.passenger_count << ' ' << mask << ' ' << int(b.together);
      break;
    }
    case IntentKind::BadRefund:
      out << "XR";
      break;
  }
  out << '\n';
}

rail::QueryRequest read_query(TokenReader& r) {
  rail::QueryRequest q{};
  q.from = r.u32();
  q.to = r.u32();
  q.earliest = r.i32();
  q.k = r.u32();
  return q;
}

uint8_t read_small(TokenReader& r, uint32_t max) {
  const uint32_t v = r.u32();
  if (v > max) r.fail("value too large: " + std::to_string(v));
  return static_cast<uint8_t>(v);
}

Intent read_intent(TokenReader& r, Workload& load) {
  const std::string tag = r.word();
  Intent in;
  if (tag == "P") {
    in.kind = IntentKind::Purchase;
    in.query = read_query(r);
    in.seat_class = read_small(r, rail::kSeatClassCount - 1);
    in.passengers = read_small(r, rail::kMaxPassengers);
    in.window_mask = read_small(r, (1u << rail::kMaxPassengers) - 1);
    in.together = read_small(r, 1) != 0;
    if (in.passengers == 0) r.fail("purchase with zero passengers");
  } else if (tag == "Q") {
    in.kind = IntentKind::Browse;
    in.query = read_query(r);
  } else if (tag == "R") {
    in.kind = IntentKind::Refund;
  } else if (tag == "RD") {
    in.kind = IntentKind::DupRefund;
  } else if (tag == "XQ") {
    in.kind = IntentKind::BadQuery;
    in.query = read_query(r);
  } else if (tag == "XB") {
    in.kind = IntentKind::BadBook;
    rail::BookRequest b{};
    b.leg_count = r.u32();
    const uint32_t listed = r.u32();
    if (listed != std::min<uint32_t>(b.leg_count, rail::kMaxLegs)) r.fail("XB listed leg count mismatch");
    for (uint32_t i = 0; i < listed; ++i) {
      b.legs[i].train = r.u32();
      b.legs[i].day = r.u32();
      b.legs[i].from = r.u32();
      b.legs[i].to = r.u32();
    }
    b.seat_class = static_cast<rail::SeatClass>(read_small(r, 255));
    b.passenger_count = r.u32();
    const uint32_t mask = read_small(r, (1u << rail::kMaxPassengers) - 1);
    for (int p = 0; p < rail::kMaxPassengers; ++p) b.window[p] = (mask >> p) & 1;
    b.together = read_small(r, 1) != 0;
    in.bad_book = static_cast<uint32_t>(load.bad_books.size());
    load.bad_books.push_back(b);
  } else if (tag == "XR") {
    in.kind = IntentKind::BadRefund;
  } else {
    r.fail("unknown intent tag " + tag);
  }
  return in;
}

}  // namespace

void write_workload(std::ostream& out, const Workload& load) {
  out << "RAILLOAD 1\n";
  out << "SCENARIO " << load.scenario << '\n';
  out << "INTENTS " << load.intents.size() << '\n';
  for (const auto& in : load.intents) write_intent(out, load, in);
  out << "QUIET " << load.quiet.size() << '\n';
  for (const auto& in : load.quiet) write_intent(out, load, in);
  out << "END\n";
}

Workload read_workload(std::istream& in) {
  TokenReader r(in, "workload");
  Workload load;
  r.expect("RAILLOAD");
  if (r.u32() != 1) r.fail("unsupported workload format version");
  r.expect("SCENARIO");
  load.scenario = r.word();
  r.expect("INTENTS");
  load.intents.resize(r.u32());
  for (auto& intent : load.intents) intent = read_intent(r, load);
  r.expect("QUIET");
  load.quiet.resize(r.u32());
  for (auto& intent : load.quiet) {
    intent = read_intent(r, load);
    if (intent.kind != IntentKind::Purchase) r.fail("quiet phase may only contain purchases");
  }
  r.expect("END");
  return load;
}

Workload load_workload(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot open workload file " + path);
  return read_workload(in);
}

void save_workload(const std::string& path, const Workload& load) {
  std::ofstream out(path, std::ios::binary);
  if (!out) throw std::runtime_error("cannot write workload file " + path);
  write_workload(out, load);
  if (!out) throw std::runtime_error("error writing workload file " + path);
}

}  // namespace railtools
