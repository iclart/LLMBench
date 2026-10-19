#include <atomic>
#include <iostream>
#include <fstream>
#include <functional>
#include <memory>
#include <stdexcept>
#include <thread>
#include <vector>
#include "ticket_system_v2.h"
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif
using namespace rail;
#define CHECK(x) do { if (!(x)) throw std::runtime_error("line " + std::to_string(__LINE__) + ": " #x); } while (false)
namespace {
void parallel(unsigned count, const std::function<void(unsigned)>& action) {
  auto completed = std::make_shared<std::atomic<unsigned>>(0);
  std::vector<std::thread> pool;
  for (unsigned i=0;i<count;++i) pool.emplace_back([=] {
    action(i);
#ifdef _WIN32
    if (completed->fetch_add(1,std::memory_order_release)+1==count) completed->notify_one();
    Sleep(INFINITE);
#endif
  });
#ifdef _WIN32
  for (auto& t:pool) t.detach();
  for (auto n=completed->load(std::memory_order_acquire);n!=count;n=completed->load(std::memory_order_acquire)) completed->wait(n);
#else
  for (auto& t:pool) t.join();
#endif
}
struct Fixture {
  Stop stops[6] = {{0,360,360},{1,400,402},{2,430,430},{1,440,440},{2,480,482},{3,520,520}};
  Train trains[2] = {{0,0,3,"A"},{0,3,3,"B"}};
  WorldData world{4,nullptr,0,nullptr,nullptr,2,trains,stops};
  std::unique_ptr<v2::TicketSystem> engine{v2::create_ticket_system()};
  explicit Fixture(bool overnight=false) {
    if(overnight){stops[0]={0,1380,1380};stops[1]={1,1445,1447};stops[2]={2,1490,1490};}
    engine->init(world, {});
  }
  BookRequest request(uint32_t t, uint32_t a, uint32_t b, uint32_t pax=1) {
    BookRequest r{}; r.leg_count=1; r.legs[0]={t,0,a,b}; r.seat_class=SeatClass::Business; r.passenger_count=pax; return r;
  }
};
void filtering_before_dedup() {
  Fixture f; BookResult result{};
  auto blocking=f.request(1,1,2);
  for (int i=0;i<24;++i) CHECK(f.engine->book(blocking,&result)==Status::Ok);
  QueryResult routes{};
  CHECK(f.engine->query({0,3,360,1,SeatClass::Business,1},&routes)==Status::Ok);
  CHECK(routes.count==1 && routes.items[0].leg_count==2);
  CHECK(routes.items[0].legs[0].to==2 && routes.items[0].legs[1].from==2);
  CHECK(f.engine->query({0,3,360,1,SeatClass::Business,0},&routes)==Status::Invalid);
}
void partial_and_exchange() {
  Fixture f; BookResult original{}, next{};
  auto request=f.request(0,0,1,2); request.leg_count=2; request.legs[1]={1,0,1,3};
  CHECK(f.engine->book(request,&original)==Status::Ok);
  auto diagonal=v2::CellMask(v2::cell(0,0)|v2::cell(1,1));
  CHECK(f.engine->refund({original.order_id,diagonal})==Status::Ok);
  CHECK(f.engine->refund({original.order_id,v2::CellMask(diagonal|v2::cell(0,1))})==Status::NotFound);
  // Failed overlapping refund did not release the other passenger-leg cells.
  CHECK(f.engine->refund({original.order_id,v2::cell(0,1)})==Status::Ok);
  CHECK(f.engine->exchange({original.order_id,v2::cell(1,0),f.request(0,0,2)},&next)==Status::Ok);
  CHECK(next.order_id!=original.order_id);
  CHECK(f.engine->refund({original.order_id,v2::cell(1,0)})==Status::NotFound);
  CHECK(f.engine->refund({next.order_id,v2::cell(0,0)})==Status::Ok);
  CHECK(f.engine->refund({next.order_id,0})==Status::Invalid);
  CHECK(f.engine->exchange({999,0x8000,f.request(0,0,1)},&next)==Status::Invalid);
}
void reuse_and_rollback() {
  Fixture f; BookResult old{}, temp{};
  auto request=f.request(0,0,2);
  CHECK(f.engine->book(request,&old)==Status::Ok);
  for(int i=1;i<24;++i) CHECK(f.engine->book(request,&temp)==Status::Ok);
  CHECK(f.engine->book(request,&temp)==Status::SoldOut);
  CHECK(f.engine->exchange({old.order_id,1,request},&temp)==Status::Ok);
  auto new_id=temp.order_id;
  auto destination=f.request(1,1,3);
  for(int i=0;i<24;++i) CHECK(f.engine->book(destination,&temp)==Status::Ok);
  CHECK(f.engine->exchange({new_id,1,destination},&temp)==Status::SoldOut);
  CHECK(f.engine->book(request,&temp)==Status::SoldOut);
  CHECK(f.engine->refund({new_id,1})==Status::Ok);
  CHECK(f.engine->book(request,&temp)==Status::Ok);
}
void failed_multileg_does_not_leak() {
  Fixture f; BookResult result{};
  for(int n=0;n<24;++n) CHECK(f.engine->book(f.request(1,1,3),&result)==Status::Ok);
  auto multi=f.request(0,0,1);multi.leg_count=2;multi.legs[1]={1,0,1,3};
  CHECK(f.engine->book(multi,&result)==Status::SoldOut);
  for(int n=0;n<24;++n) CHECK(f.engine->book(f.request(0,0,1),&result)==Status::Ok);
}
void cross_day_and_fragments() {
  Fixture overnight(true); QueryResult paths{};
  CHECK(overnight.engine->query({0,3,1370,1,SeatClass::Business,1},&paths)==Status::Ok);
  CHECK(paths.count==1 && paths.items[0].leg_count==2);
  CHECK(paths.items[0].legs[0].day==0 && paths.items[0].legs[0].arrive>1440);
  CHECK(paths.items[0].legs[1].day==1 && paths.items[0].legs[1].arrive==1960);
  Fixture f;BookResult first[24]{},second[24]{},result{};
  for(int i=0;i<24;++i){
    CHECK(f.engine->book(f.request(0,0,1),&first[i])==Status::Ok);
    CHECK(f.engine->book(f.request(0,1,2),&second[i])==Status::Ok);
  }
  auto same=[](const Seat& a,const Seat& b){return a.car==b.car && a.row==b.row && a.letter==b.letter;};
  for(int i=0;i<12;++i)CHECK(f.engine->refund({first[i].order_id,1})==Status::Ok);
  int bridging=-1;
  for(int i=0;i<24;++i){
    bool first_half=false;
    for(int j=0;j<12;++j)first_half|=same(second[i].seats[0][0],first[j].seats[0][0]);
    if(!first_half)CHECK(f.engine->refund({second[i].order_id,1})==Status::Ok);
    else bridging=i;
  }
  // Every interval has twelve free seats, but none is free for the full ride.
  CHECK(f.engine->book(f.request(0,0,2),&result)==Status::SoldOut);
  CHECK(bridging>=0 && f.engine->refund({second[bridging].order_id,1})==Status::Ok);
  CHECK(f.engine->book(f.request(0,0,2),&result)==Status::Ok);
}
void concurrency() {
  Fixture f; BookResult original{};
  CHECK(f.engine->book(f.request(0,0,2,2),&original)==Status::Ok);
  std::atomic<int> wins{0};
  parallel(8,[&](unsigned){ if(f.engine->refund({original.order_id,1})==Status::Ok) ++wins; });
  CHECK(wins==1);
  CHECK(f.engine->refund({original.order_id,v2::cell(1,0)})==Status::Ok);
  CHECK(f.engine->book(f.request(0,0,2,5),&original)==Status::Ok);
  std::atomic<int> disjoint{0};
  parallel(5,[&](unsigned i){if(f.engine->refund({original.order_id,v2::cell(i,0)})==Status::Ok)++disjoint;});
  CHECK(disjoint==5);
  std::atomic<int> errors{0};
  parallel(8,[&](unsigned i){
    auto r=f.request(uint32_t(i%2),uint32_t(i%2),uint32_t(i%2?3:2));
    for(int n=0;n<100;++n){ BookResult b{};
      if(f.engine->book(r,&b)!=Status::Ok || f.engine->refund({b.order_id,1})!=Status::Ok) ++errors;
    }
  });
  CHECK(errors==0);
  // All resources are free after concurrent updates to distinct snapshot pages/blocks.
  for(int train=0;train<2;++train)for(int n=0;n<24;++n){BookResult b{}; CHECK(f.engine->book(f.request(train,train?1:0,train?3:2),&b)==Status::Ok);}
}
}
int main(int argc,char** argv) {
  const char* names[]={"realtime_query","partial_and_exchange","reuse_and_rollback","multileg_rollback","cross_day_and_fragments","concurrency"};
  void(*tests[])()={filtering_before_dedup,partial_and_exchange,reuse_and_rollback,failed_multileg_does_not_leak,cross_day_and_fragments,concurrency};
  std::string only, path;
  for(int i=1;i<argc;++i) {
    if(std::string(argv[i])=="--case" && i+1<argc) only=argv[++i];
    else if(std::string(argv[i])=="--json" && i+1<argc) path=argv[++i];
    else return 2;
  }
  bool failed=false, first=true; std::string json="{\"categories\":{";
  for(int i=0;i<6;++i) {
    if(!only.empty() && only!=names[i]) continue;
    bool passed=true;
    try { tests[i](); } catch(const std::exception& e) { passed=false;failed=true;std::cerr<<names[i]<<": "<<e.what()<<'\n'; }
    if(!first) json+=',';
    first=false;
    json+='"'+std::string(names[i])+"\":{\"passed\":"+(passed?"1":"0")+",\"total\":1}";
  }
  if(first) return 2;
  json+="},\"crashes\":0}";
  if(!path.empty()){std::ofstream out(path);out<<json<<'\n';if(!out)return 2;}
  std::cout<<json<<'\n';std::cout.flush();std::cerr.flush();
#ifdef _WIN32
  ExitProcess(failed?1:0);
#endif
  return failed?1:0;
}
