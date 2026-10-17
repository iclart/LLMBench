// Seat layout (TASK.md 2.3) and seat-preference rules (TASK.md 5.4) as used
// by the tools. Seat indices run 0..kSeatsPerTrainDay-1 within a train-day,
// class by class, car by car, row by row, letter by letter.
#pragma once

#include <cstdint>
#include <cstdio>
#include <string>

#include "ticket_system.h"

namespace railtools {

struct ClassInfo {
  int first_car, car_count, rows;
  const char* letters;     // letters of one row, left to right
  int aisle_after;         // number of letters left of the aisle
  int first_seat;
  int seats() const { return car_count * rows * row_width(); }
  int row_width() const { int n = 0; while (letters[n]) ++n; return n; }
  int largest_block() const { return aisle_after > row_width() - aisle_after ? aisle_after : row_width() - aisle_after; }
};

inline constexpr ClassInfo kClasses[rail::kSeatClassCount] = {
    {1, 1, 8, "ACF", 1, 0},
    {2, 2, 13, "ACDF", 2, 24},
    {4, 5, 18, "ABCDF", 3, 128},
};

inline const ClassInfo& class_info(rail::SeatClass c) { return kClasses[static_cast<int>(c)]; }

// Returns the seat index, or -1 if `s` is not a seat of class `c`.
inline int seat_index(rail::SeatClass c, const rail::Seat& s) {
  const ClassInfo& k = class_info(c);
  if (s.car < k.first_car || s.car >= k.first_car + k.car_count) return -1;
  if (s.row < 1 || s.row > k.rows) return -1;
  int col = -1;
  for (int i = 0; k.letters[i]; ++i)
    if (k.letters[i] == s.letter) col = i;
  if (col < 0) return -1;
  return k.first_seat + ((s.car - k.first_car) * k.rows + (s.row - 1)) * k.row_width() + col;
}

inline bool is_window(const rail::Seat& s) { return s.letter == 'A' || s.letter == 'F'; }

// Column of the seat within its row (0-based), assuming it is valid for `c`.
inline int seat_column(rail::SeatClass c, const rail::Seat& s) {
  const ClassInfo& k = class_info(c);
  for (int i = 0; k.letters[i]; ++i)
    if (k.letters[i] == s.letter) return i;
  return -1;
}

// TASK.md 5.4 "together" rule for one leg; seats must be valid for `c`.
inline bool seated_together(rail::SeatClass c, const rail::Seat* seats, int n) {
  const ClassInfo& k = class_info(c);
  int min_row = seats[0].row, max_row = seats[0].row;
  for (int i = 1; i < n; ++i) {
    if (seats[i].car != seats[0].car) return false;
    if (seats[i].row < min_row) min_row = seats[i].row;
    if (seats[i].row > max_row) max_row = seats[i].row;
  }
  if (n > k.row_width()) return max_row - min_row <= 1;
  if (min_row != max_row) return false;
  if (n > k.largest_block()) return true;
  int min_col = seat_column(c, seats[0]), max_col = min_col;
  for (int i = 1; i < n; ++i) {
    const int col = seat_column(c, seats[i]);
    if (col < min_col) min_col = col;
    if (col > max_col) max_col = col;
  }
  // Distinct seats spanning exactly n columns are contiguous; same side of the aisle.
  const bool same_side = (max_col < k.aisle_after) || (min_col >= k.aisle_after);
  return same_side && max_col - min_col == n - 1;
}

inline std::string seat_name(const rail::Seat& s) {
  char buf[16];
  std::snprintf(buf, sizeof buf, "%02d-%02d%c", s.car, s.row, s.letter);
  return buf;
}

}  // namespace railtools
