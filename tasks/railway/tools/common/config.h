// Minimal `key = integer` config files for the railway tools.
//
// Lines are `key = value`; `#` starts a comment. Values are signed 64-bit
// integers (probabilities are written as permille). Every key that a
// generator reads must be present, and every key in the file must be read,
// so typos fail loudly instead of silently falling back to a default.
#pragma once

#include <cstdint>
#include <map>
#include <set>
#include <string>

namespace railtools {

class Config {
 public:
  static Config load(const std::string& path);
  static Config parse(const std::string& text, const std::string& origin);

  int64_t get(const std::string& key);
  int64_t get(const std::string& key, int64_t lo, int64_t hi);
  bool has(const std::string& key) const { return values_.count(key) != 0; }

  // Throws if some key in the file was never read.
  void check_all_used() const;

 private:
  std::string origin_;
  std::map<std::string, int64_t> values_;
  std::set<std::string> used_;
};

}  // namespace railtools
