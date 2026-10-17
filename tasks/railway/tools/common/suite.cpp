#include "suite.h"

#include <algorithm>
#include <fstream>
#include <ostream>
#include <sstream>
#include <stdexcept>

#include "answers.h"
#include "rules.h"
#include "seats.h"
#include "text_io.h"

namespace railtools {

namespace {

uint64_t stop_mask(const ResolvedLeg& l) {
  return ((uint64_t(1) << l.to_index) - 1) & ~((uint64_t(1) << l.from_index) - 1);
}

std::string status_name(rail::Status s) {
  switch (s) {
    case rail::Status::Ok: return "Ok";
    case rail::Status::SoldOut: return "SoldOut";
    case rail::Status::Invalid: return "Invalid";
    case rail::Status::NotFound: return "NotFound";
    case rail::Status::Withheld: return "Withheld";
  }
  return "status " + std::to_string(static_cast<int>(s));
}

std::string describe(const rail::BookRequest& b, uint32_t listed) {
  std::string s = "book";
  for (uint32_t i = 0; i < listed; ++i) {
    const auto& l = b.legs[i];
    s += " [train " + std::to_string(l.train) + " day " + std::to_string(l.day) + " " + std::to_string(l.from) +
         "->" + std::to_string(l.to) + "]";
  }
  return s + " class " + std::to_string(int(b.seat_class)) + " x" + std::to_string(b.passenger_count);
}

}  // namespace

std::vector<SuiteCase> load_suite(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot read " + path);
  std::string line;
  auto fail = [&](const std::string& msg) { throw std::runtime_error(path + ": " + msg + " in: " + line); };
  auto next_line = [&]() {
    if (!std::getline(f, line)) return false;
    if (!line.empty() && line.back() == '\r') line.pop_back();
    return true;
  };
  if (!next_line() || line != "RAILSUITE 1") fail("not a RAILSUITE 1 file");
  std::vector<SuiteCase> cases;
  SuiteCase* cur = nullptr;
  size_t declared = 0;
  bool ended = false;
  while (next_line()) {
    if (line.empty()) continue;
    std::istringstream in(line);
    TokenReader r(in, path);
    const std::string tag = r.word();
    if (tag == "CASES") {
      declared = r.u32();
    } else if (tag == "C") {
      if (cur) fail("case not ended");
      cases.push_back({r.word(), {}});
      cur = &cases.back();
    } else if (tag == "E") {
      if (!cur) fail("E outside a case");
      cur = nullptr;
    } else if (tag == "END") {
      ended = true;
      break;
    } else if (!cur) {
      fail("operation outside a case");
    } else if (tag == "Q") {
      SuiteOp op;
      op.kind = 'Q';
      op.query.from = r.u32();
      op.query.to = r.u32();
      op.query.earliest = r.i32();
      op.query.k = r.u32();
      const size_t bar = line.find(" | ");
      if (bar == std::string::npos) fail("query without an answer");
      op.answer = line.substr(bar + 3);
      cur->ops.push_back(op);
    } else if (tag == "B") {
      SuiteOp op;
      op.kind = 'B';
      op.book.leg_count = r.u32();
      op.listed = r.u32();
      if (op.listed > uint32_t(rail::kMaxLegs)) fail("too many legs listed");
      for (uint32_t i = 0; i < op.listed; ++i) {
        op.book.legs[i].train = r.u32();
        op.book.legs[i].day = r.u32();
        op.book.legs[i].from = r.u32();
        op.book.legs[i].to = r.u32();
      }
      op.book.seat_class = static_cast<rail::SeatClass>(r.u32() & 0xff);
      op.book.passenger_count = r.u32();
      const uint32_t mask = r.u32();
      for (int p = 0; p < rail::kMaxPassengers; ++p) op.book.window[p] = (mask >> p) & 1;
      op.book.together = r.u32() != 0;
      std::string label;
      if (r.try_word(label)) {
        if (label.size() < 2 || label[0] != '@') fail("bad label");
        op.label = label.substr(1);
      }
      cur->ops.push_back(op);
    } else if (tag == "R") {
      SuiteOp op;
      op.kind = 'R';
      const std::string target = r.word();
      if (target.size() >= 2 && target[0] == '@') {
        op.label = target.substr(1);
      } else if (target.size() >= 2 && target[0] == '#') {
        op.order_id = std::stoull(target.substr(1));
      } else {
        fail("refund needs @label or #id");
      }
      cur->ops.push_back(op);
    } else {
      fail("unknown tag " + tag);
    }
  }
  if (!ended || cur) fail("missing END");
  if (cases.size() != declared) fail("CASES says " + std::to_string(declared) + ", found " + std::to_string(cases.size()));
  return cases;
}

void write_suite(std::ostream& out, const std::vector<SuiteCase>& cases) {
  out << "RAILSUITE 1\nCASES " << cases.size() << '\n';
  for (const auto& c : cases) {
    out << "C " << c.category << '\n';
    for (const auto& op : c.ops) {
      if (op.kind == 'Q') {
        out << "Q " << op.query.from << ' ' << op.query.to << ' ' << op.query.earliest << ' ' << op.query.k << " | "
            << op.answer << '\n';
      } else if (op.kind == 'B') {
        const auto& b = op.book;
        out << "B " << b.leg_count << ' ' << op.listed;
        for (uint32_t i = 0; i < op.listed; ++i)
          out << ' ' << b.legs[i].train << ' ' << b.legs[i].day << ' ' << b.legs[i].from << ' ' << b.legs[i].to;
        uint32_t mask = 0;
        for (int p = 0; p < rail::kMaxPassengers; ++p) mask |= uint32_t(b.window[p]) << p;
        out << ' ' << int(b.seat_class) << ' ' << b.passenger_count << ' ' << mask << ' ' << int(b.together);
        if (!op.label.empty()) out << " @" << op.label;
        out << '\n';
      } else {
        out << "R " << (op.label.empty() ? "#" + std::to_string(op.order_id) : "@" + op.label) << '\n';
      }
    }
    out << "E\n";
  }
  out << "END\n";
}

std::vector<uint64_t>& SuiteRunner::seats_of(uint64_t td) {
  auto& v = occ_[td];
  if (v.empty()) v.assign(rail::kSeatsPerTrainDay, 0);
  return v;
}

std::string SuiteRunner::run(SuiteCase& c, bool record) {
  std::unordered_map<std::string, Order> orders;
  for (size_t i = 0; i < c.ops.size(); ++i) {
    SuiteOp& op = c.ops[i];
    std::string err;
    if (op.kind == 'Q') err = query(op, record);
    else if (op.kind == 'B') err = book(op, orders);
    else err = refund(op, orders);
    if (!err.empty()) return "step " + std::to_string(i + 1) + ": " + err;
  }
  return "";
}

std::string SuiteRunner::query(SuiteOp& op, bool record) {
  rail::QueryResult r{};
  const rail::Status st = sys_.query(op.query, &r);
  std::string got;
  append_answer(got, st, r);
  if (record) {
    op.answer = got;
    return "";
  }
  if (got == op.answer) return "";
  const auto& q = op.query;
  return "query from " + std::to_string(q.from) + " to " + std::to_string(q.to) + " earliest " +
         std::to_string(q.earliest) + " k " + std::to_string(q.k) + "\n      got      " + got +
         "\n      expected " + op.answer;
}

std::string SuiteRunner::book(const SuiteOp& op, std::unordered_map<std::string, Order>& orders) {
  const rail::BookRequest& b = op.book;
  rail::BookResult res{};
  const rail::Status st = sys_.book(b, &res);
  const std::string what = describe(b, op.listed);
  if (!is_valid_book(w_, b)) {
    return st == rail::Status::Invalid ? "" : what + ": malformed, expected Invalid, got " + status_name(st);
  }
  if (st == rail::Status::Invalid) return what + ": valid request rejected as Invalid";
  if (st == rail::Status::Withheld) return "";  // a sales policy: allowed
  const ClassInfo& ci = class_info(b.seat_class);
  const int n = static_cast<int>(b.passenger_count);
  ResolvedLeg legs[rail::kMaxLegs];
  uint64_t tds[rail::kMaxLegs];
  for (uint32_t j = 0; j < b.leg_count; ++j) {
    resolve_leg(w_, b.legs[j], &legs[j]);
    tds[j] = uint64_t(b.legs[j].train) * rail::kDays + b.legs[j].day;
  }
  if (st == rail::Status::SoldOut) {
    for (uint32_t j = 0; j < b.leg_count; ++j) {
      const auto& seats = seats_of(tds[j]);
      int free = 0;
      for (int k = ci.first_seat; k < ci.first_seat + ci.seats(); ++k) free += (seats[k] & stop_mask(legs[j])) == 0;
      if (free < n) return "";
    }
    return what + ": SoldOut although every leg had enough free seats";
  }
  if (st != rail::Status::Ok) return what + ": unexpected status " + status_name(st);
  if (res.order_id == 0 || !issued_.insert(res.order_id).second) {
    return what + ": order id " + std::to_string(res.order_id) + " is zero or reused";
  }
  Order order{res.order_id, true, {}};
  for (uint32_t j = 0; j < b.leg_count; ++j) {
    auto& seats = seats_of(tds[j]);
    const uint64_t mask = stop_mask(legs[j]);
    std::vector<int> taken;
    for (int p = 0; p < n; ++p) {
      const rail::Seat& s = res.seats[j][p];
      const int idx = seat_index(b.seat_class, s);
      if (idx < 0) return what + ": seat " + seat_name(s) + " is not a seat of the requested class";
      if (std::find(taken.begin(), taken.end(), idx) != taken.end()) {
        return what + ": seat " + seat_name(s) + " given to two passengers";
      }
      if (seats[idx] & mask) return what + ": seat " + seat_name(s) + " sold twice for overlapping intervals";
      taken.push_back(idx);
    }
    for (int idx : taken) {
      seats[idx] |= mask;
      order.holds.push_back({tds[j], idx, mask});
    }
  }
  if (!op.label.empty()) orders[op.label] = order;
  return "";
}

std::string SuiteRunner::refund(const SuiteOp& op, std::unordered_map<std::string, Order>& orders) {
  Order* order = nullptr;
  uint64_t id = op.order_id;
  if (!op.label.empty()) {
    const auto it = orders.find(op.label);
    if (it == orders.end()) return "";  // its book did not succeed (SoldOut or Withheld)
    order = &it->second;
    id = order->id;
  }
  const rail::Status st = sys_.refund(id);
  const rail::Status want = order && order->live ? rail::Status::Ok : rail::Status::NotFound;
  if (st != want) {
    return "refund of order " + std::to_string(id) + (order && order->live ? " (live)" : " (not live)") +
           ": expected " + status_name(want) + ", got " + status_name(st);
  }
  if (order && order->live) {
    order->live = false;
    for (const Hold& h : order->holds) occ_[h.td][h.seat] &= ~h.mask;
  }
  return "";
}

}  // namespace railtools
