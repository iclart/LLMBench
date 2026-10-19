// Deliberately broken engines, used only to prove the checking seams reject
// missing queries, incorrect partial release, non-atomic exchange and leaks.
#define create_ticket_system create_inner_system
#include "../reference/baseline.cpp"
#undef create_ticket_system
namespace rail::v2 {
namespace {
class Mutant final : public TicketSystem {
  std::unique_ptr<TicketSystem> inner_{create_inner_system()};
  std::unique_ptr<tools::Problem> problem_;
  std::mutex mutex_;
  std::unordered_map<uint64_t,CellMask> active_;
 public:
  void init(const WorldData& w,const Options& options) override {
    inner_->init(w,options);problem_=std::make_unique<tools::Problem>(w);
  }
  Status query(const QueryRequest& q,QueryResult* out) override {
    std::lock_guard<std::mutex> lock(mutex_);
    auto status=inner_->query(q,out);
#if RAIL_V2_MUTANT == 1
    if(status==Status::Ok) out->count=0;
#elif RAIL_V2_MUTANT == 6
    if(status==Status::Ok) *out=tools::exhaustive_query(*problem_,q,[](const tools::Resolved&){return true;});
#endif
    return status;
  }
  Status book(const BookRequest& r,BookResult* out) override {
    std::lock_guard<std::mutex> lock(mutex_);
    auto status=inner_->book(r,out);
#if RAIL_V2_MUTANT == 4
    if(status==Status::SoldOut && r.leg_count>1){auto first=r;first.leg_count=1;BookResult leaked{};inner_->book(first,&leaked);}
#endif
    if(status==Status::Ok) active_[out->order_id]=all_cells(r.passenger_count,r.leg_count);
    return status;
  }
  Status refund(const RefundRequest& r) override {
    std::lock_guard<std::mutex> lock(mutex_);
#if RAIL_V2_MUTANT == 7
    auto found=active_.find(r.order_id);
    if(found!=active_.end() && r.cells && (found->second&r.cells)==r.cells){found->second&=~r.cells;return Status::Ok;}
#endif
    auto status=inner_->refund(r);
    if(status==Status::Ok){
      active_[r.order_id]&=~r.cells;
#if RAIL_V2_MUTANT == 2
      auto remaining=active_[r.order_id];
      if(remaining){CellMask extra=CellMask(remaining&-remaining);inner_->refund({r.order_id,extra});active_[r.order_id]&=~extra;}
#endif
    }
#if RAIL_V2_MUTANT == 5
    if(status==Status::NotFound) return Status::Ok;
#endif
    return status;
  }
  Status exchange(const ExchangeRequest& r,BookResult* out) override {
    std::lock_guard<std::mutex> lock(mutex_);
#if RAIL_V2_MUTANT == 3
    auto status=inner_->refund({r.order_id,r.cells});
    if(status!=Status::Ok)return status;
    status=inner_->book(r.replacement,out);
#else
    auto status=inner_->exchange(r,out);
#endif
    if(status==Status::Ok){active_[r.order_id]&=~r.cells;active_[out->order_id]=all_cells(r.replacement.passenger_count,r.replacement.leg_count);}
    return status;
  }
};
}
TicketSystem* create_ticket_system(){return new Mutant();}
}
