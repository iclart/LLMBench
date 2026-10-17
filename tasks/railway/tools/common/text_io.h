// Whitespace-separated token reader for the railway text formats.
#pragma once

#include <cstdint>
#include <istream>
#include <stdexcept>
#include <string>

namespace railtools {

class TokenReader {
 public:
  TokenReader(std::istream& in, std::string what) : in_(in), what_(std::move(what)) {}

  // Returns false at end of input.
  bool try_word(std::string& out) { return static_cast<bool>(in_ >> out); }

  std::string word() {
    std::string w;
    if (!try_word(w)) fail("unexpected end of input");
    return w;
  }

  void expect(const char* keyword) {
    const std::string w = word();
    if (w != keyword) fail(std::string("expected ") + keyword + ", got " + w);
  }

  int64_t i64() {
    const std::string w = word();
    size_t pos = 0;
    int64_t v = 0;
    try {
      v = std::stoll(w, &pos);
    } catch (const std::exception&) {
      pos = 0;
    }
    if (pos == 0 || pos != w.size()) fail("expected an integer, got " + w);
    return v;
  }

  uint32_t u32() {
    const int64_t v = i64();
    if (v < 0 || v > UINT32_MAX) fail("value out of uint32 range: " + std::to_string(v));
    return static_cast<uint32_t>(v);
  }

  int32_t i32() {
    const int64_t v = i64();
    if (v < INT32_MIN || v > INT32_MAX) fail("value out of int32 range: " + std::to_string(v));
    return static_cast<int32_t>(v);
  }

  [[noreturn]] void fail(const std::string& msg) { throw std::runtime_error(what_ + ": " + msg); }

 private:
  std::istream& in_;
  std::string what_;
};

}  // namespace railtools
